"""SQLite 图存储。

## 为什么要换掉 JSON 落盘

`InMemoryGraphStore` 的持久化是「**每次 save 全量序列化整个图谱再覆写文件**」：

| 问题 | 后果 |
| --- | --- |
| 全量重写 | 10 万关系的图每次入库都要序列化+写盘全部 10 万条 |
| 非原子 | 写一半崩溃 → 文件损坏 → 下次启动「加载失败，从空图启动」，**全部图谱丢失** |
| 全量读入内存 | 启动时整个图驻留内存；多副本各自持一份，互不同步 |
| 无并发保护 | 多进程同时写必然互相覆盖 |

SQLite 解决前三条（事务原子性、增量写、按需查询），代价是每次查询要走 SQL
而不是内存指针——但图谱查询是「按实体名找邻边」，有索引后是 O(度)，
与内存版同级。

## 设计取舍

**列结构**：`tenant_id` / `name` 等**查询用字段**建独立列并加索引；其余属性
（aliases / description / degree / extra）存 JSON 列。理由是这些字段只读不查，
为它们建列会让 schema 演进变成迁移负担，而本项目正处于快速迭代期。

**不使用 ORM**：这里只需要 3 张表与少量固定查询，手写 SQL 更透明，
也避免引入额外依赖与"ORM 生成的 SQL 不可预测"的问题。

**`save()` 是空操作**：数据在每次写操作时就已提交，不存在"忘记保存"的可能——
这是相比 JSON 版最实质的改进。
"""
import json
import logging
import sqlite3
from collections import deque
import threading
from pathlib import Path

from app.domain.schemas import Community, EntityNode, RelationEdge

logger = logging.getLogger(__name__)

MAX_BFS_EDGES = 50

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    tenant_id TEXT NOT NULL,
    name      TEXT NOT NULL,
    payload   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, name)
);
CREATE TABLE IF NOT EXISTS relations (
    tenant_id  TEXT NOT NULL,
    source     TEXT NOT NULL,
    relation   TEXT NOT NULL,
    target     TEXT NOT NULL,
    weight     REAL NOT NULL DEFAULT 1.0,
    payload    TEXT NOT NULL,
    PRIMARY KEY (tenant_id, source, relation, target)
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relations (tenant_id, source);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relations (tenant_id, target);
CREATE TABLE IF NOT EXISTS communities (
    community_id TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comm_tenant ON communities (tenant_id);
"""


class SqliteGraphStore:
    """图存储的 SQLite 实现（满足 `BaseGraphStore` 契约）。

    线程安全：内部一把锁串行化写（SQLite 单写者语义），读走同一连接。
    对图谱这种"写少读多、单机量级"的场景足够；真要高并发写应换服务端图库。
    """

    def __init__(self, path: Path | str, k1: float = 0.0):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL：读不阻塞写（图查询频繁、入库偶发，这个取舍很值）
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---- 写入 ----

    def upsert_entity(self, entity: EntityNode) -> None:
        tenant = entity.tenant_id or "default"
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM entities WHERE tenant_id=? AND name=?",
                (tenant, entity.name),
            ).fetchone()
            if row:
                # 幂等合并：保留已有描述，别名取并集（与内存版语义一致）
                old = EntityNode(**json.loads(row["payload"]))
                if entity.description and not old.description:
                    old.description = entity.description
                for alias in entity.aliases:
                    if alias not in old.aliases:
                        old.aliases.append(alias)
                merged = old
            else:
                merged = entity
            self._conn.execute(
                "INSERT INTO entities (tenant_id, name, payload) VALUES (?,?,?) "
                "ON CONFLICT(tenant_id, name) DO UPDATE SET payload=excluded.payload",
                (tenant, entity.name, merged.model_dump_json()),
            )
            self._commit()

    def upsert_relation(self, rel: RelationEdge) -> None:
        tenant = rel.tenant_id or "default"
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, weight FROM relations "
                "WHERE tenant_id=? AND source=? AND relation=? AND target=?",
                (tenant, rel.source, rel.relation, rel.target),
            ).fetchone()
            if row:
                old = RelationEdge(**json.loads(row["payload"]))
                for c in rel.source_chunks:
                    if c not in old.source_chunks:
                        old.source_chunks.append(c)
                # 强度累加封顶（与内存版一致：多条证据 → 更强）
                new_weight = min(2.0, float(row["weight"]) + 0.2 * len(rel.source_chunks))
                # ⚠️ weight 同时存在于列（供 SQL 排序）与 payload（反序列化的真值）。
                # 两处必须同步更新，否则"列已更新、读出来还是旧值"——这个 bug 会表现为
                # "同一关系反复写入但强度永远不变"，且很难从结果反推原因。
                old.weight = new_weight
                self._conn.execute(
                    "UPDATE relations SET payload=?, weight=? "
                    "WHERE tenant_id=? AND source=? AND relation=? AND target=?",
                    (old.model_dump_json(), new_weight, tenant, rel.source, rel.relation, rel.target),
                )
            else:
                self._conn.execute(
                    "INSERT INTO relations (tenant_id, source, relation, target, weight, payload) "
                    "VALUES (?,?,?,?,?,?)",
                    (tenant, rel.source, rel.relation, rel.target,
                     float(rel.weight), rel.model_dump_json()),
                )
            self._refresh_degree(tenant, rel.source)
            self._refresh_degree(tenant, rel.target)
            self._commit()

    def delete_doc_chunks(self, chunk_ids: set[str]) -> int:
        """按块删除其贡献的关系（以块为最小更新单位）。

        只删除"该块是唯一来源"的关系；仍有其它块支撑的关系只摘掉这个块。
        """
        if not chunk_ids:
            return 0
        removed = 0
        with self._lock:
            rows = self._conn.execute("SELECT * FROM relations").fetchall()
            for row in rows:
                rel = RelationEdge(**json.loads(row["payload"]))
                if not (set(rel.source_chunks) & chunk_ids):
                    continue
                remaining = [c for c in rel.source_chunks if c not in chunk_ids]
                if remaining:
                    rel.source_chunks = remaining
                    self._conn.execute(
                        "UPDATE relations SET payload=? WHERE tenant_id=? AND source=? "
                        "AND relation=? AND target=?",
                        (rel.model_dump_json(), row["tenant_id"], row["source"],
                         row["relation"], row["target"]),
                    )
                    continue
                self._conn.execute(
                    "DELETE FROM relations WHERE tenant_id=? AND source=? AND relation=? AND target=?",
                    (row["tenant_id"], row["source"], row["relation"], row["target"]),
                )
                removed += 1
            # 清理孤立实体（仍在社区摘要里的实体保留——摘要是独立产物）
            member_names = {
                m
                for r in self._conn.execute("SELECT payload FROM communities").fetchall()
                for m in json.loads(r["payload"]).get("members", [])
            }
            for row in self._conn.execute("SELECT tenant_id, name FROM entities").fetchall():
                has_edge = self._conn.execute(
                    "SELECT 1 FROM relations WHERE tenant_id=? AND (source=? OR target=?) LIMIT 1",
                    (row["tenant_id"], row["name"], row["name"]),
                ).fetchone()
                if not has_edge and row["name"] not in member_names:
                    self._conn.execute(
                        "DELETE FROM entities WHERE tenant_id=? AND name=?",
                        (row["tenant_id"], row["name"]),
                    )
            self._commit()
        return removed

    def _refresh_degree(self, tenant: str, name: str) -> None:
        row = self._conn.execute(
            "SELECT payload FROM entities WHERE tenant_id=? AND name=?", (tenant, name)
        ).fetchone()
        if not row:
            return
        entity = EntityNode(**json.loads(row["payload"]))
        counts = self._conn.execute(
            "SELECT (SELECT COUNT(*) FROM relations WHERE tenant_id=? AND source=?) + "
            "(SELECT COUNT(*) FROM relations WHERE tenant_id=? AND target=?) AS degree",
            (tenant, name, tenant, name),
        ).fetchone()
        entity.degree = int(counts["degree"]) if counts else 0
        self._conn.execute(
            "UPDATE entities SET payload=? WHERE tenant_id=? AND name=?",
            (entity.model_dump_json(), tenant, name),
        )

    def _commit(self) -> None:
        self._conn.commit()

    # ---- 查询 ----

    def get_entity(self, name: str, tenant_id: str | None = None) -> EntityNode | None:
        with self._lock:
            if tenant_id is not None:
                row = self._conn.execute(
                    "SELECT payload FROM entities WHERE tenant_id=? AND name=?",
                    (tenant_id or "default", name),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT payload FROM entities WHERE name=? LIMIT 1", (name,)
                ).fetchone()
        return EntityNode(**json.loads(row["payload"])) if row else None

    def neighbors(
        self, name: str, depth: int = 2, max_edges: int = MAX_BFS_EDGES,
        tenant_id: str | None = None,
    ) -> list[dict]:
        """BFS（先近后远），返回带方向的关系路径项。

        **精确对齐 `InMemoryGraphStore` 的既有语义**（契约一致性测试会逐字段比对）：

        - `direction` 取值是 `forward` / `backward`（注意：`BaseGraphStore` 的
          docstring 写的是 out/in，**与实现不符**——以实现为准，因为前端与
          `graph/query.py` 都依赖实际输出）；
        - 返回项含 `via` / `other` / `description`；
        - **同一关系不去重**：一条边会从两端各出现一次（forward + backward）。
          这看起来冗余，但社区检测与路径展示依赖"从当前节点出发"的重复计数，
          存储层私自去重会静默改变图谱行为。
        - `weight` 四舍五入到 3 位。
        """
        tenant = tenant_id or "default"
        if self.get_entity(name, tenant) is None:
            return []
        results: list[dict] = []
        visited = {name}
        queue = deque([(name, 0)])
        with self._lock:
            while queue and len(results) < max_edges:
                current, d = queue.popleft()
                if d >= depth:
                    continue
                edges = self._edges_of(tenant, current)
                edges.sort(key=lambda pair: pair[0].weight, reverse=True)
                for edge, forward in edges:
                    other = edge.target if forward else edge.source
                    results.append({
                        "from": edge.source,
                        "to": edge.target,
                        "relation": edge.relation,
                        "direction": "forward" if forward else "backward",
                        "via": current,
                        "other": other,
                        "weight": round(edge.weight, 3),
                        "description": edge.description,
                        "source_chunks": list(edge.source_chunks),
                        "depth": d + 1,
                    })
                    if other not in visited:
                        visited.add(other)
                        queue.append((other, d + 1))
        return results

    def _edges_of(self, tenant: str, name: str) -> list[tuple[RelationEdge, bool]]:
        """返回 [(edge, 是否以 name 为 source)]；出边在前（与内存版拼接顺序一致）。"""
        out = [
            (RelationEdge(**json.loads(r["payload"])), True)
            for r in self._conn.execute(
                "SELECT payload FROM relations WHERE tenant_id=? AND source=?", (tenant, name)
            ).fetchall()
        ]
        inc = [
            (RelationEdge(**json.loads(r["payload"])), False)
            for r in self._conn.execute(
                "SELECT payload FROM relations WHERE tenant_id=? AND target=?", (tenant, name)
            ).fetchall()
        ]
        return out + inc

    def all_entities(self, tenant_id: str | None = None) -> list[EntityNode]:
        with self._lock:
            if tenant_id is not None:
                rows = self._conn.execute(
                    "SELECT payload FROM entities WHERE tenant_id=?", (tenant_id or "default",)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT payload FROM entities").fetchall()
        return [EntityNode(**json.loads(r["payload"])) for r in rows]

    def all_relations(self, tenant_id: str | None = None) -> list[RelationEdge]:
        with self._lock:
            if tenant_id is not None:
                rows = self._conn.execute(
                    "SELECT payload FROM relations WHERE tenant_id=?", (tenant_id or "default",)
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT payload FROM relations").fetchall()
        return [RelationEdge(**json.loads(r["payload"])) for r in rows]

    def set_community(self, community: Community) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO communities (community_id, tenant_id, payload) VALUES (?,?,?) "
                "ON CONFLICT(community_id) DO UPDATE SET payload=excluded.payload",
                (community.community_id, community.tenant_id or "default",
                 community.model_dump_json()),
            )
            self._commit()

    def communities(self, tenant_id: str | None = None) -> list[Community]:
        with self._lock:
            if tenant_id is not None:
                rows = self._conn.execute(
                    "SELECT payload FROM communities WHERE tenant_id=?",
                    (tenant_id or "default",),
                ).fetchall()
            else:
                rows = self._conn.execute("SELECT payload FROM communities").fetchall()
        return [Community(**json.loads(r["payload"])) for r in rows]

    def stats(self, tenant_id: str | None = None) -> dict:
        """与内存版**同构**的统计（字段不一致会让前端与契约测试一起失效）。"""
        tenant = tenant_id or "default"
        with self._lock:
            ent = self._conn.execute(
                "SELECT COUNT(*) AS c FROM entities WHERE tenant_id=?", (tenant,)
            ).fetchone()["c"]
            rel = self._conn.execute(
                "SELECT COUNT(*) AS c FROM relations WHERE tenant_id=?", (tenant,)
            ).fetchone()["c"]
            comm = self._conn.execute(
                "SELECT COUNT(*) AS c FROM communities WHERE tenant_id=?", (tenant,)
            ).fetchone()["c"]
            orphan = self._conn.execute(
                "SELECT COUNT(*) AS c FROM entities e WHERE e.tenant_id=? AND NOT EXISTS ("
                "  SELECT 1 FROM relations r WHERE r.tenant_id=e.tenant_id"
                "  AND (r.source=e.name OR r.target=e.name))",
                (tenant,),
            ).fetchone()["c"]
        return {
            "entities": int(ent),
            "relations": int(rel),
            "communities": int(comm),
            "orphan_ratio": round(int(orphan) / max(int(ent), 1), 4),
        }

    def save(self) -> None:
        """空操作：数据在每次写操作时已提交（ 相比 JSON 全量落盘的核心改进）。"""

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("图存储关闭失败（忽略）: %s", exc)


def build_graph_store(settings, data_dir: Path):
    """按配置构造图存储。

    - `graph_store_backend=sqlite`（默认）：SQLite 增量持久化
    - `graph_store_backend=memory`：内存 + JSON 全量落盘（旧行为，兼容与测试）

    **自动迁移**：选 sqlite 但只存在旧 JSON 文件时，先把 JSON 导入 SQLite，
    再把 JSON 改名备份——避免升级后"图谱看起来被清空了"。
    """
    from app.services.graph.store import InMemoryGraphStore

    backend = str(getattr(settings, "graph_store_backend", "sqlite") or "sqlite").lower()
    json_path = Path(getattr(settings, "graph_path", "") or (data_dir / "graph" / "graph.json"))
    if backend != "sqlite":
        return InMemoryGraphStore(json_path)
    db_path = json_path.with_suffix(".db")
    store = SqliteGraphStore(db_path)
    if json_path.exists():
        _migrate_json_to_sqlite(json_path, store)
    return store


def _migrate_json_to_sqlite(json_path: Path, store: SqliteGraphStore) -> None:
    """把旧 JSON 图谱导入 SQLite，成功后把 JSON 改名备份（不删除）。"""
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        n_e = n_r = 0
        for e in payload.get("entities", []):
            store.upsert_entity(EntityNode(**e))
            n_e += 1
        for r in payload.get("relations", []):
            store.upsert_relation(RelationEdge(**r))
            n_r += 1
        for c in payload.get("communities", []):
            store.set_community(Community(**c))
        json_path.rename(json_path.with_suffix(".json.migrated"))
        logger.info("图谱已从 JSON 迁移到 SQLite：%d 实体 / %d 关系", n_e, n_r)
    except Exception as exc:  # noqa: BLE001 - 迁移失败不应阻断启动
        logger.warning("图谱 JSON 迁移失败（继续使用 SQLite 空库）: %s", exc)
