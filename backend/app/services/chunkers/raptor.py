"""RAPTOR 递归摘要。

L0 原始块 → 聚类 → 每簇生成 L1 摘要 → 对 L1 再聚类 → …直到顶层 ≤ max_top_nodes。
所有层级共同进入向量索引：抽象问题命中高层摘要，细节问题命中低层叶子。

成本显式承认：每层聚类 + LLM 摘要都是 token 开销，只在 ingestion 阶段跑一次，
检索路径零额外开销。长文档（chunk 多）才值得启用。
"""
import logging

import numpy as np

from app.domain.schemas import Chunk
from app.services.embeddings import Embeddings
from app.services.llm import LLM

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = """请用 100 字以内概括以下内容的共同主题与关键信息，直接输出摘要，不要解释：

{content}"""


def _kmeans(vectors: np.ndarray, k: int, seed: int = 42) -> np.ndarray:
    """轻量 k-means（numpy 实现，避免引入 sklearn）。返回每个点的簇编号。"""
    n = len(vectors)
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)
    centroids = vectors[rng.choice(n, k, replace=False)].copy()
    labels = np.zeros(n, dtype=int)
    for _ in range(20):
        sims = vectors @ centroids.T  # 向量已归一化，内积即余弦
        new_labels = sims.argmax(axis=1)
        if (new_labels == labels).all():
            break
        labels = new_labels
        for c in range(k):
            members = vectors[labels == c]
            if len(members):
                centroid = members.mean(axis=0)
                norm = np.linalg.norm(centroid)
                centroids[c] = centroid / norm if norm else centroid
    return labels


class RaptorChunker:
    name = "raptor"

    def __init__(self, embeddings: Embeddings, llm: LLM):
        self._embeddings = embeddings
        self._llm = llm

    def build_tree(
        self,
        doc_id: str,
        leaf_chunks: list[Chunk],
        max_clusters: int = 4,
        max_top_nodes: int = 3,
    ) -> list[Chunk]:
        """在 L0 叶子块之上递归构建摘要层，返回 叶子 + 全部摘要块。"""
        all_chunks = list(leaf_chunks)
        current_level_nodes = list(leaf_chunks)
        level = 0
        while len(current_level_nodes) > max_top_nodes:
            level += 1
            vectors = np.array(
                self._embeddings.embed_texts([c.content for c in current_level_nodes]),
                dtype=np.float32,
            )
            k = min(max_clusters, max(1, len(current_level_nodes) // max_top_nodes))
            k = max(k, min(max_top_nodes, len(current_level_nodes)))
            labels = _kmeans(vectors, k)

            next_level: list[Chunk] = []
            for cluster_id in sorted(set(labels)):
                members = [c for c, lb in zip(current_level_nodes, labels) if lb == cluster_id]
                summary_text = self._llm.generate(
                    SUMMARY_PROMPT.format(content="\n".join(m.content[:500] for m in members))
                ).strip()
                if not summary_text:
                    summary_text = members[0].content[:100]
                summary_id = f"{doc_id}#L{level}-{cluster_id + 1}"
                summary_chunk = Chunk(
                    chunk_id=summary_id,
                    doc_id=doc_id,
                    seq=len(all_chunks) + 1,
                    content=summary_text,
                    tenant_id=members[0].tenant_id,
                    source=f"RAPTOR L{level} 摘要（含 {len(members)} 个子节点）",
                    chunker=self.name,
                    level=level,
                )
                # 子节点回指摘要块（层级 + 父块标识必须同时存在）
                for m in members:
                    m.parent_id = summary_id
                    m.chunker = self.name
                next_level.append(summary_chunk)
                all_chunks.append(summary_chunk)
            current_level_nodes = next_level
            logger.info(
                "RAPTOR L%d 完成: %d → %d（doc=%s）", level, len(labels), len(next_level), doc_id
            )
        return all_chunks
