"""实体消歧。

归一化是图质量的命门：同一实体的不同叫法必须归并到同一节点，
否则多跳检索必然断链。

合并规则（按优先级）：
1. 规范化精确匹配（去空格/大小写/全半角）→ 直接合并（确定性路径）；
2. 别名表命中 → 合并；
3. 向量相似度 ≥ 阈值（经验值 0.85）→ 合并；
硬约束：**只在同类型之间合并**——同名的公司与产品合并在多跳检索中会被放大成错误。

租户纪律（对照分析文档 KQ-06）：实体、别名、向量索引全部按租户分区。
不同租户的同名词互不消歧，也不互相成为检索种子。
"""
import logging
import unicodedata

import numpy as np

from app.services.embeddings import Embeddings
from app.services.graph.vector_index import EntityVectorIndex

logger = logging.getLogger(__name__)


def normalize_name(name: str) -> str:
    """名称规范化：全半角统一 + 去空白 + 小写。"""
    return unicodedata.normalize("NFKC", name).strip().lower().replace(" ", "")


def _k(tenant_id: str, name: str) -> str:
    return f"{tenant_id}\x00{name}"


class EntityResolver:
    def __init__(self, embeddings: Embeddings, sim_threshold: float = 0.85):
        self._embeddings = embeddings
        self._threshold = sim_threshold
        # {tenant\x00name} → {name, type, aliases, vector}
        self._entities: dict[str, dict] = {}
        # {tenant\x00规范化名/别名} → {tenant\x00规范名}
        self._alias_map: dict[str, str] = {}
        # 租户 → 向量索引（消歧从 O(N) 次 Python 点积降为一次矩阵乘）
        self._indexes: dict[str, EntityVectorIndex] = {}
        # 类型表缓存（按租户；避免每次消歧都重建 name→type 映射）
        self._type_caches: dict[str, dict[str, str]] = {}
        self._type_cache_lens: dict[str, int] = {}

    def _index_of(self, tenant_id: str) -> EntityVectorIndex:
        index = self._indexes.get(tenant_id)
        if index is None:
            index = EntityVectorIndex()
            self._indexes[tenant_id] = index
        return index

    def _type_of(self, tenant_id: str) -> dict[str, str]:
        """name → type（传给索引做类型过滤）。惰性重建，避免每次消歧都扫全表。"""
        size = sum(1 for key in self._entities if key.startswith(f"{tenant_id}\x00"))
        cache = self._type_caches.get(tenant_id)
        if self._type_cache_lens.get(tenant_id) != size or cache is None:
            prefix = f"{tenant_id}\x00"
            cache = {
                key[len(prefix):]: info["type"]
                for key, info in self._entities.items()
                if key.startswith(prefix)
            }
            self._type_caches[tenant_id] = cache
            self._type_cache_lens[tenant_id] = size
        return cache

    def resolve(
        self, name: str, etype: str, description: str = "", tenant_id: str = "default"
    ) -> tuple[str, bool]:
        """归一化一个实体名。返回 (规范名, 是否新建)。"""
        key = normalize_name(name)
        # 1. 精确/别名命中（确定性路径）
        alias_key = _k(tenant_id, key)
        if alias_key in self._alias_map:
            canonical_key = self._alias_map[alias_key]
            if self._entities[canonical_key]["type"] == etype:
                return canonical_key.split("\x00", 1)[1], False
        # 2. 向量相似度合并（仅同类型，§5.4 硬约束）
        vec = np.array(self._embeddings.embed_query(name), dtype=np.float32)
        # 改为向量化索引查询（此前是逐实体 Python 循环点积）。
        # 索引只做"最相似"的检索，类型过滤在此传入——保持消歧的类型硬约束。
        best_name, best_sim = None, 0.0
        hits = self._index_of(tenant_id).search(
            vec, top_k=1, threshold=0.0,
            type_of=self._type_of(tenant_id), want_type=etype,
        )
        if hits:
            best_name, best_sim = hits[0]
        if best_name is not None and best_sim >= self._threshold:
            info = self._entities[_k(tenant_id, best_name)]
            if name != best_name and name not in info["aliases"]:
                info["aliases"].append(name)
                self._alias_map[alias_key] = _k(tenant_id, best_name)
            if description and not info["description"]:
                info["description"] = description
            return best_name, False
        # 3. 新建实体
        self._entities[_k(tenant_id, name)] = {
            "name": name, "type": etype, "description": description,
            "aliases": [], "vector": vec,
        }
        self._alias_map[alias_key] = _k(tenant_id, name)
        self._index_of(tenant_id).add(name, vec)  # 同步进向量索引
        return name, True

    def search_similar(
        self, query_vector: np.ndarray, top_k: int = 1, threshold: float = 0.0,
        want_type: str | None = None, tenant_id: str = "default",
    ) -> list[tuple[str, float]]:
        """按向量召回实体（种子召回复用同一向量化索引）。

        与 `resolve` 共用索引，因此不重复编码、不重复维护矩阵。
        """
        return self._index_of(tenant_id).search(
            query_vector, top_k=top_k, threshold=threshold,
            type_of=self._type_of(tenant_id) if want_type else None, want_type=want_type,
        )

    def suspend_index(self, tenant_id: str = "default") -> None:
        """批量入库前调用：暂存向量，避免逐条扩容矩阵。"""
        self._index_of(tenant_id).suspend()

    def resume_index(self, tenant_id: str = "default") -> None:
        """批量入库后调用：一次性落矩阵。"""
        self._index_of(tenant_id).resume()

    def lookup(self, name: str, tenant_id: str = "default") -> str | None:
        """按任意叫法查规范名（种子实体匹配用）。"""
        found = self._alias_map.get(_k(tenant_id, normalize_name(name)))
        return found.split("\x00", 1)[1] if found else None

    def vector_of(self, name: str, tenant_id: str = "default") -> np.ndarray | None:
        """实体的已存向量（图检索复用，避免重复编码）。"""
        info = self._entities.get(_k(tenant_id, name))
        return info["vector"] if info else None

    def all_entities(self, tenant_id: str | None = None) -> list[dict]:
        """实体清单（tenant_id 为 None 时返回全部，仅运维/导出用）。"""
        result = []
        for key, info in self._entities.items():
            if tenant_id is not None and not key.startswith(f"{tenant_id}\x00"):
                continue
            result.append({
                "name": info["name"], "type": info["type"], "description": info["description"],
                "aliases": list(info["aliases"]),
            })
        return result

    def export(self) -> dict:
        return {
            "entities": {
                key: {**info, "vector": info["vector"].tolist()}
                for key, info in self._entities.items()
            },
            "alias_map": dict(self._alias_map),
        }

    @classmethod
    def load(cls, payload: dict, embeddings: Embeddings, sim_threshold: float) -> "EntityResolver":
        resolver = cls(embeddings, sim_threshold)
        for key, info in (payload.get("entities") or {}).items():
            resolver._entities[key] = {**info, "vector": np.array(info["vector"], dtype=np.float32)}
        resolver._alias_map = dict(payload.get("alias_map") or {})
        # KQ-41：必须重建向量索引——只恢复实体表会让重启后的向量消歧与
        # 种子向量召回静默失效（索引是空的，resolve 退化为"全部新建"）。
        for key, info in resolver._entities.items():
            tenant_id = key.split("\x00", 1)[0] if "\x00" in key else "default"
            name = key.split("\x00", 1)[1] if "\x00" in key else key
            resolver._index_of(tenant_id).add(name, info["vector"])
        return resolver
