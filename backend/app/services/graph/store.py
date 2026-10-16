"""图存储抽象与内存实现（阶段 7 §2.3）。

- 接口抽象：测试用内存图，生产可换 Neo4j / Redis，互不污染；
- 内存实现 + JSON 落盘（重启可恢复）；
- neighbors 为 BFS（先近后远，§6.2），关系按强度降序截断；
- 关系有方向：in_edges / out_edges 分开维护，遍历时保留方向信息。

关于换存储引擎：此前 `InMemoryGraphStore` 是唯一实现，且 `builder` / `query` /
`community` 三处直接把它写进类型签名，换引擎要同时改三个文件。现在抽出
`BaseGraphStore` 抽象，只要新引擎满足该协议，这三处无需改动。
"""
import json
import logging
import threading
from collections import deque
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.domain.schemas import Community, EntityNode, RelationEdge

logger = logging.getLogger(__name__)

MAX_BFS_EDGES = 50  # 子图爆炸护栏：单次遍历最多返回的关系数


@runtime_checkable
class BaseGraphStore(Protocol):
    """图存储契约。

    抽出这层不是为了"看起来像有架构"，而是为了让 `GraphBuilder` / `GraphQuery` /
    `CommunityDetector` 三个消费者只依赖契约——它们的类型签名里出现的是这个
    Protocol，而不是某个具体引擎。

    语义约定（实现方必须遵守，否则上层的社区摘要与溯源会失效）：
    - `upsert_entity` / `upsert_relation` 按 `name` / `(source, relation, target)`
      幂等，重复写入是合并而非追加；
    - `neighbors` 返回的每条边必须带 `direction`（out / in），否则前端画不出箭头方向；
    - `delete_doc_chunks` 只删"当前版本"的块对应实体，历史版本块仍需留在图里（§6.3）；
    - 线程安全：所有写操作内部加锁。
    """

    def upsert_entity(self, entity: EntityNode) -> None: ...
    def upsert_relation(self, rel: RelationEdge) -> None: ...
    def delete_doc_chunks(self, chunk_ids: set[str]) -> int: ...
    def get_entity(self, name: str) -> EntityNode | None: ...
    def neighbors(
        self, name: str, depth: int = 2, max_edges: int = MAX_BFS_EDGES
    ) -> list[dict]: ...
    def all_entities(self) -> list[EntityNode]: ...
    def all_relations(self) -> list[RelationEdge]: ...
    def set_community(self, community: Community) -> None: ...
    def communities(self) -> list[Community]: ...
    def stats(self) -> dict: ...
    def save(self) -> None: ...


_SEP = "\x1f"  # 单元分隔符：实体名里不会出现，用它做租户分区的 key 连接符


def _k(tenant_id: str | None, name: str) -> str:
    """租户分区的复合 key。

    TASK-142 / P0-4：此前 `_entities` / `_out` / `_in` 都是全局单层 dict 且 key 只有实体名，
    导致 ① 不同租户的同名实体互相覆盖 ② 任一租户可看到全量图谱。
    改为 (tenant, name) 复合 key 后，两者同时解决。
    """
    return f"{tenant_id or 'default'}{_SEP}{name}"


class InMemoryGraphStore:
    """邻接表内存图。出边/入边分索引，BFS 按强度剪枝。"""

    def __init__(self, path: Path | None = None):
        self._entities: dict[str, EntityNode] = {}
        self._out: dict[str, list[RelationEdge]] = {}  # source → edges
        self._in: dict[str, list[RelationEdge]] = {}  # target → edges
        self._communities: dict[str, Community] = {}
        self._lock = threading.Lock()
        self._path = path
        if path and path.exists():
            self._load(path)

    # ---- 写入 ----

    def upsert_entity(self, entity: EntityNode) -> None:
        with self._lock:
            key = _k(entity.tenant_id, entity.name)
            existing = self._entities.get(key)
            if existing:
                if entity.description and not existing.description:
                    existing.description = entity.description
                for alias in entity.aliases:
                    if alias not in existing.aliases:
                        existing.aliases.append(alias)
            else:
                self._entities[key] = entity

    def upsert_relation(self, rel: RelationEdge) -> None:
        with self._lock:
            out_edges = self._out.setdefault(_k(rel.tenant_id, rel.source), [])
            for e in out_edges:
                if e.target == rel.target and e.relation == rel.relation:
                    # 同关系追加溯源块与强度（多条证据 → 强度累加封顶）
                    for c in rel.source_chunks:
                        if c not in e.source_chunks:
                            e.source_chunks.append(c)
                    e.weight = min(2.0, e.weight + 0.2 * len(rel.source_chunks))
                    return
            out_edges.append(rel)
            self._in.setdefault(_k(rel.tenant_id, rel.target), []).append(rel)
            self._touch_degree(rel.tenant_id, rel.source)
            self._touch_degree(rel.tenant_id, rel.target)

    def _touch_degree(self, tenant_id: str, name: str) -> None:
        key = _k(tenant_id, name)
        entity = self._entities.get(key)
        if entity:
            entity.degree = len(self._out.get(key, [])) + len(self._in.get(key, []))

    def delete_doc_chunks(self, chunk_ids: set[str]) -> int:
        """按块删除其贡献的关系（以块为最小更新单位，§5.6）。"""
        removed = 0
        with self._lock:
            for key, rels in list(self._out.items()):
                for rel in list(rels):
                    before = len(rel.source_chunks)
                    rel.source_chunks = [c for c in rel.source_chunks if c not in chunk_ids]
                    if rel.source_chunks and len(rel.source_chunks) != before:
                        continue
                    if not rel.source_chunks:
                        rels.remove(rel)
                        self._in.get(_k(rel.tenant_id, rel.target), []).remove(rel)
                        removed += 1
            # 清理无任何关系的孤立实体
            for key, entity in list(self._entities.items()):
                if not self._out.get(key) and not self._in.get(key):
                    if any(entity.name in c.members for c in self._communities.values()):
                        continue
                    del self._entities[key]
        return removed

    # ---- 查询 ----

    def get_entity(self, name: str, tenant_id: str | None = None) -> EntityNode | None:
        if tenant_id is not None:
            return self._entities.get(_k(tenant_id, name))
        # 未指定租户：按名字跨租户命中第一份（内部构建/兼容路径用）
        for entity in self._entities.values():
            if entity.name == name:
                return entity
        return None

    def neighbors(
        self, name: str, depth: int = 2, max_edges: int = MAX_BFS_EDGES,
        tenant_id: str | None = None,
    ) -> list[dict]:
        """BFS 遍历（先近后远）。返回带方向的关系路径项。

        传 tenant_id 时全程限定在该租户分区内（P0-4：不得跨租户遍历）。
        """
        root_key = _k(tenant_id, name)
        if root_key not in self._entities:
            return []
        results: list[dict] = []
        visited = {root_key}
        queue = deque([(name, 0)])
        while queue and len(results) < max_edges:
            current, d = queue.popleft()
            if d >= depth:
                continue
            key = _k(tenant_id, current)
            edges = sorted(
                self._out.get(key, []) + self._in.get(key, []),
                key=lambda e: -e.weight,
            )
            for edge in edges:
                forward = edge.source == current
                other = edge.target if forward else edge.source
                results.append({
                    "from": edge.source, "to": edge.target,
                    "relation": edge.relation,
                    "direction": "forward" if forward else "backward",
                    "via": current, "other": other,
                    "weight": round(edge.weight, 3),
                    "description": edge.description,
                    "source_chunks": edge.source_chunks,
                    "depth": d + 1,
                })
                other_key = _k(tenant_id, other)
                if other_key not in visited:
                    visited.add(other_key)
                    queue.append((other, d + 1))
        return results

    def all_entities(self, tenant_id: str | None = None) -> list[EntityNode]:
        entities = [
            e for e in self._entities.values()
            if tenant_id is None or e.tenant_id == tenant_id
        ]
        return sorted(entities, key=lambda e: -e.degree)

    def all_relations(self, tenant_id: str | None = None) -> list[RelationEdge]:
        return [
            e for edges in self._out.values() for e in edges
            if tenant_id is None or e.tenant_id == tenant_id
        ]

    # ---- 社区 ----

    def set_community(self, community: Community) -> None:
        with self._lock:
            self._communities[community.community_id] = community
            for name in community.members:
                entity = self._entities.get(_k(community.tenant_id, name))
                if entity:
                    entity.community_id = community.community_id

    def communities(self, tenant_id: str | None = None) -> list[Community]:
        return sorted(
            (c for c in self._communities.values() if tenant_id is None or c.tenant_id == tenant_id),
            key=lambda c: c.community_id,
        )

    # ---- 统计 ----

    def stats(self, tenant_id: str | None = None) -> dict:
        entities = self.all_entities(tenant_id)
        relations = self.all_relations(tenant_id)
        orphan = sum(1 for e in entities if e.degree == 0)
        return {
            "entities": len(entities),
            "relations": len(relations),
            "communities": len(self.communities(tenant_id)),
            "orphan_ratio": round(orphan / max(len(entities), 1), 4),
        }

    # ---- 持久化 ----

    def save(self) -> None:
        if not self._path:
            return
        with self._lock:
            payload = {
                "entities": [e.model_dump() for e in self._entities.values()],
                "relations": [r.model_dump() for r in self.all_relations()],
                "communities": [c.model_dump() for c in self._communities.values()],
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _load(self, path: Path) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            for e in payload.get("entities", []):
                entity = EntityNode(**e)
                self._entities[_k(entity.tenant_id, entity.name)] = entity
            for r in payload.get("relations", []):
                rel = RelationEdge(**r)
                self._out.setdefault(_k(rel.tenant_id, rel.source), []).append(rel)
                self._in.setdefault(_k(rel.tenant_id, rel.target), []).append(rel)
            for c in payload.get("communities", []):
                self._communities[c["community_id"]] = Community(**c)
            logger.info("图存储已加载: %d 实体 / %d 关系", len(self._entities), len(self.all_relations()))
        except Exception:
            logger.exception("图存储加载失败，从空图启动")
