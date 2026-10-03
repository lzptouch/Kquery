"""切块器工厂（阶段 3 §1.1 Chunker Factory）。

按 chunk_policy 选择策略并产出块：
- recursive（默认）：零额外开销、可预测；
- semantic：需要 embedding（相邻句子距离阈值切）；
- parent_child：返回 (子块入向量库, 父块入 DocStore)；
- raptor：先递归切出 L0，再构建多层摘要树（长文档才值得启用）。

产物统一：(chunks, parents, chunker_name)。
"""
from dataclasses import dataclass, field

from app.domain.schemas import Chunk
from app.services.chunkers.parent_child import ParentChildChunker
from app.services.chunkers.raptor import RaptorChunker
from app.services.chunkers.semantic import SemanticChunker
from app.services.chunking import build_chunks
from app.services.embeddings import Embeddings
from app.services.llm import LLM

DEFAULT_POLICY: dict = {"strategy": "recursive", "params": {}}


@dataclass
class ChunkResult:
    chunks: list[Chunk] = field(default_factory=list)  # 全部入向量库的块
    parents: list[Chunk] = field(default_factory=list)  # 入 DocStore 的父块（父子分块）
    chunker: str = "recursive"


def apply_chunk_policy(
    doc_id: str,
    segments: list[tuple[str, str]],
    tenant_id: str,
    policy: dict,
    embeddings: Embeddings,
    llm: LLM,
    chunk_size: int = 300,
    chunk_overlap: int = 50,
    min_chars: int = 20,
) -> ChunkResult:
    strategy = policy.get("strategy", "recursive")
    params = policy.get("params", {})

    if strategy == "semantic":
        chunker = SemanticChunker(embeddings)
        chunks = chunker.chunk(
            doc_id, segments, tenant_id,
            threshold=float(params.get("threshold", 0.4)),
            min_sentences=int(params.get("min_sentences", 2)),
            max_sentences=int(params.get("max_sentences", 12)),
            min_chars=min_chars,
        )
        return ChunkResult(chunks=chunks, chunker="semantic")

    if strategy == "parent_child":
        children, parents = ParentChildChunker().chunk(
            doc_id, segments, tenant_id,
            parent_size=int(params.get("parent_size", 1200)),
            child_size=int(params.get("child_size", 200)),
            child_overlap=int(params.get("child_overlap", 40)),
            min_chars=min_chars,
        )
        return ChunkResult(chunks=children, parents=parents, chunker="parent_child")

    if strategy == "raptor":
        # 先切出 L0 叶子，再构建摘要树
        leaves = build_chunks(
            doc_id, segments, tenant_id,
            chunk_size=int(params.get("chunk_size", chunk_size)),
            overlap=int(params.get("chunk_overlap", chunk_overlap)),
            min_chars=min_chars,
        )
        for leaf in leaves:
            leaf.chunker = "raptor"
        all_chunks = RaptorChunker(embeddings, llm).build_tree(
            doc_id, leaves,
            max_clusters=int(params.get("max_clusters", 4)),
            max_top_nodes=int(params.get("max_top_nodes", 3)),
        )
        return ChunkResult(chunks=all_chunks, chunker="raptor")

    # 默认：递归切块
    chunks = build_chunks(doc_id, segments, tenant_id, chunk_size, chunk_overlap, min_chars)
    return ChunkResult(chunks=chunks, chunker="recursive")


def validate_chunk_policy(policy: dict) -> str | None:
    """向导第 3 步的入参校验（36xx 错误码），返回错误信息或 None。"""
    strategy = policy.get("strategy", "recursive")
    if strategy not in {"recursive", "semantic", "parent_child", "raptor"}:
        return f"未知的切块策略: {strategy}"
    params = policy.get("params", {})
    if strategy == "semantic":
        threshold = float(params.get("threshold", 0.4))
        if not 0.0 < threshold < 1.0:
            return "semantic.threshold 必须在 (0, 1) 之间"
        if int(params.get("min_sentences", 2)) < 1:
            return "semantic.min_sentences 必须 ≥ 1"
        if int(params.get("max_sentences", 12)) <= int(params.get("min_sentences", 2)):
            return "semantic.max_sentences 必须大于 min_sentences"
    if strategy == "parent_child":
        parent = int(params.get("parent_size", 1200))
        child = int(params.get("child_size", 200))
        if child >= parent:
            return "parent_child.child_size 必须小于 parent_size"
        if child < 50:
            return "parent_child.child_size 不能小于 50"
    if strategy == "raptor":
        if int(params.get("max_clusters", 4)) < 2:
            return "raptor.max_clusters 必须 ≥ 2"
    return None
