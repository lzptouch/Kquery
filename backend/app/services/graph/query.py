"""图检索服务（阶段 7 §3）。

- 种子实体：混合策略——精确/别名命中 ∪ 向量召回（Top-3 上限）；
- 遍历：BFS 深度 2（默认），按强度剪枝，子图过大护栏；
- 失败模式：实体未命中 → 返回空由 Agent 回退向量检索，不允许直接判无答案；
- 输出：带方向的关系路径文本（"A —投资→ B"），附支撑块溯源。
"""
import logging

import numpy as np

from app.services.embeddings import Embeddings
from app.services.graph.resolver import EntityResolver, normalize_name
from app.services.graph.store import InMemoryGraphStore

logger = logging.getLogger(__name__)

SEED_TOP_K = 3  # 种子实体上限（防子图爆炸）
SEED_VECTOR_TH = 0.5  # 向量召回种子的最低相似度（召回宁宽，BFS 再剪）


class GraphQueryService:
    def __init__(
        self,
        store: InMemoryGraphStore,
        resolver: EntityResolver,
        embeddings: Embeddings,
        bfs_depth: int = 2,
    ):
        self._store = store
        self._resolver = resolver
        self._embeddings = embeddings
        self._depth = bfs_depth

    def find_seeds(self, question: str) -> list[str]:
        """混合种子：精确/别名命中 ∪ 实体向量召回，Top-3。"""
        seeds: list[str] = []
        # 1. 精确/别名命中：问题中包含实体名（或别名）即命中
        for info in self._resolver.all_entities():
            names = [info["name"], *info["aliases"]]
            if any(n and n in question for n in names):
                if info["name"] not in seeds:
                    seeds.append(info["name"])
            if len(seeds) >= SEED_TOP_K:
                return seeds
        # 2. 向量召回（问题未出现确切实体名时；复用消歧器已存向量，不重复编码）
        if not seeds:
            try:
                qvec = np.array(self._embeddings.embed_query(question), dtype=np.float32)
                scored = []
                for info in self._resolver.all_entities():
                    evec = self._resolver.vector_of(info["name"])
                    if evec is None or evec.shape != qvec.shape:
                        continue
                    sim = float(np.dot(qvec, evec))
                    if sim >= SEED_VECTOR_TH:
                        scored.append((info["name"], sim))
                scored.sort(key=lambda x: -x[1])
                seeds = [name for name, _ in scored[:SEED_TOP_K]]
            except Exception as exc:  # noqa: BLE001
                logger.warning("种子向量召回失败: %s", exc)
        return seeds[:SEED_TOP_K]

    def search(self, question: str, tenant_id: str = "default") -> dict:
        """图检索主入口。返回 {seeds, paths, evidence_items}。"""
        seeds = self.find_seeds(question)
        if not seeds:
            return {"seeds": [], "paths": [], "evidence_items": []}

        paths: list[dict] = []
        seen_edges: set[tuple[str, str, str]] = set()
        for seed in seeds:
            for item in self._store.neighbors(seed, depth=self._depth):
                key = (item["from"], item["to"], item["relation"])
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                paths.append(item)

        evidence_items = [
            {
                "chunk_id": f"graph#{p['from']}->{p['to']}",
                "content": (
                    f"{p['from']} —{p['relation']}→ {p['to']}"
                    + (f"（{p['description'][:100]}）" if p["description"] else "")
                ),
                "score": round(p["weight"] / max(p["depth"], 1), 4),
                "source": f"知识图谱 · {p['relation']}",
                "doc_id": "__graph__",
                "source_route": "graph",
                "modality": "graph",
                "source_chunks": p["source_chunks"],
            }
            for p in paths
        ]
        return {"seeds": seeds, "paths": paths, "evidence_items": evidence_items}
