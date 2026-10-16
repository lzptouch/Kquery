"""实体消歧（阶段 7 §2.2 / §5.4）。

归一化是图质量的命门：同一实体的不同叫法必须归并到同一节点，
否则多跳检索必然断链。

合并规则（按优先级）：
1. 规范化精确匹配（去空格/大小写/全半角）→ 直接合并（确定性路径）；
2. 别名表命中 → 合并；
3. 向量相似度 ≥ 阈值（经验值 0.85）→ 合并；
硬约束：**只在同类型之间合并**——同名的公司与产品合并在多跳检索中会被放大成错误。
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


class EntityResolver:
    def __init__(self, embeddings: Embeddings, sim_threshold: float = 0.85):
        self._embeddings = embeddings
        self._threshold = sim_threshold
        # 规范名 → {name, type, aliases, vector}
        self._entities: dict[str, dict] = {}
        # 规范化名/别名 → 规范名
        self._alias_map: dict[str, str] = {}
        # TASK-153：向量化索引（消歧从 O(N) 次 Python 点积降为一次矩阵乘）
        self._index = EntityVectorIndex()
        # 类型表缓存（避免每次消歧都重建 name→type 映射）
        self._type_cache: dict[str, str] | None = None
        self._type_cache_len = -1

    def _type_of(self) -> dict[str, str]:
        """name → type（传给索引做类型过滤）。惰性重建，避免每次消歧都扫全表。"""
        if (
            self._type_cache_len != len(self._entities)
            or self._type_cache is None
        ):
            self._type_cache = {n: i["type"] for n, i in self._entities.items()}
            self._type_cache_len = len(self._entities)
        return self._type_cache

    def resolve(self, name: str, etype: str, description: str = "") -> tuple[str, bool]:
        """归一化一个实体名。返回 (规范名, 是否新建)。"""
        key = normalize_name(name)
        # 1. 精确/别名命中（确定性路径）
        if key in self._alias_map:
            canonical = self._alias_map[key]
            if self._entities[canonical]["type"] == etype:
                return canonical, False
        # 2. 向量相似度合并（仅同类型，§5.4 硬约束）
        vec = np.array(self._embeddings.embed_query(name), dtype=np.float32)
        # TASK-153：改为向量化索引查询（此前是逐实体 Python 循环点积）。
        # 索引只做"最相似"的检索，类型过滤在此传入——保持消歧的类型硬约束。
        best_name, best_sim = None, 0.0
        hits = self._index.search(
            vec, top_k=1, threshold=0.0,
            type_of=self._type_of(), want_type=etype,
        )
        if hits:
            best_name, best_sim = hits[0]
        if best_name is not None and best_sim >= self._threshold:
            info = self._entities[best_name]
            if name != best_name and name not in info["aliases"]:
                info["aliases"].append(name)
                self._alias_map[key] = best_name
            if description and not info["description"]:
                info["description"] = description
            return best_name, False
        # 3. 新建实体
        self._entities[name] = {
            "name": name, "type": etype, "description": description,
            "aliases": [], "vector": vec,
        }
        self._alias_map[key] = name
        self._index.add(name, vec)  # TASK-153：同步进向量索引
        return name, True

    def search_similar(
        self, query_vector: np.ndarray, top_k: int = 1, threshold: float = 0.0,
        want_type: str | None = None,
    ) -> list[tuple[str, float]]:
        """按向量召回实体（TASK-153：种子召回复用同一向量化索引）。

        与 `resolve` 共用索引，因此不重复编码、不重复维护矩阵。
        """
        return self._index.search(
            query_vector, top_k=top_k, threshold=threshold,
            type_of=self._type_of() if want_type else None, want_type=want_type,
        )

    def suspend_index(self) -> None:
        """批量入库前调用：暂存向量，避免逐条扩容矩阵。"""
        self._index.suspend()

    def resume_index(self) -> None:
        """批量入库后调用：一次性落矩阵。"""
        self._index.resume()

    def lookup(self, name: str) -> str | None:
        """按任意叫法查规范名（种子实体匹配用）。"""
        return self._alias_map.get(normalize_name(name))

    def vector_of(self, name: str) -> np.ndarray | None:
        """实体的已存向量（图检索复用，避免重复编码）。"""
        info = self._entities.get(name)
        return info["vector"] if info else None

    def all_entities(self) -> list[dict]:
        return [
            {"name": i["name"], "type": i["type"], "description": i["description"],
             "aliases": list(i["aliases"])}
            for i in self._entities.values()
        ]

    def export(self) -> dict:
        return {
            "entities": {
                name: {**info, "vector": info["vector"].tolist()}
                for name, info in self._entities.items()
            },
            "alias_map": dict(self._alias_map),
        }

    @classmethod
    def load(cls, payload: dict, embeddings: Embeddings, sim_threshold: float) -> "EntityResolver":
        resolver = cls(embeddings, sim_threshold)
        for name, info in (payload.get("entities") or {}).items():
            resolver._entities[name] = {**info, "vector": np.array(info["vector"], dtype=np.float32)}
        resolver._alias_map = dict(payload.get("alias_map") or {})
        return resolver
