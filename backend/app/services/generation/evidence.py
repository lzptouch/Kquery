"""证据装配（`B0 X-27` 第一步：从 `services/pipeline.py` 的 `_to_evidence` 抽出）。

**边界原则**（设计稿 §9.5）：这些函数**不持有编排器的可变状态**，依赖一律由参数
传入（`name_of` / `parent_of` 是回调，不是一个 pipeline 引用）。这样每一步都能用
假候选直接单测，也避免「拆分只是把耦合换了位置」。

装配链路：候选 → 父块展开/去重 → `assemble_context`（MMR + token 预算）→ `Evidence`。
"""
from typing import Callable

from app.core.logging_setup import get_logger
from app.domain.schemas import Evidence
from app.services.context import ContextItem
from app.services.retrieval.hybrid import HybridResult

logger = get_logger(__name__)


def asset_uri(chunk) -> str:
    """文本块关联图像的**资源名**（前端拼 /graph/assets/{name} 取图）。

    只给文件名、不给绝对路径：绝对路径会把服务端目录结构暴露给前端，
    而且换部署目录就失效。
    """
    assets = list(getattr(chunk, "related_assets", []) or [])
    if not assets:
        return ""
    # asset_id 形如 "doc#page-2" / "doc#img-1"；资源文件名由入库时决定，
    # 这里用 doc_id + 序号拼出与 assets 目录一致的名字。
    doc_id = getattr(chunk, "doc_id", "") or ""
    for asset_id in assets:
        tail = str(asset_id).split("#", 1)[-1]
        if tail.startswith("page-"):
            return f"{doc_id}#page-{tail.split('-', 1)[1]}.png"
        if tail.startswith("img-"):
            return f"{doc_id}-img{tail.split('-', 1)[1]}"
    return ""


def avg_score(evidence: list[Evidence], top_n: int) -> tuple[float, int]:
    """Top-N 重排分均值与样本数（以 ACL 过滤后的可见证据为口径）。"""
    scores = sorted((e.score for e in evidence if e.score is not None), reverse=True)
    scores = scores[:top_n]
    if not scores:
        return 0.0, 0
    return sum(scores) / len(scores), len(scores)


def build_candidates(
    hybrid_result: HybridResult,
    *,
    name_of: Callable[[str], str],
    parent_of: Callable[[str], object] | None = None,
) -> tuple[list[ContextItem], dict[str, tuple]]:
    """候选 → `ContextItem`（含父块展开与同一父块去重）。

    返回 `(candidates, meta)`：`meta` 保留原始 chunk / 候选 / 来源标签，避免组装后
    再反查（去重键 → `(chunk, cand, source_label)`）。

    `parent_of` 为 `None` 表示没有文档存储（内存/精简部署），此时不展开父块。
    """
    visible = list(hybrid_result.candidates)

    # 先构造候选（含父块展开），再统一做
    # 去重 → MMR → token 预算裁剪 → 硬截断。
    # 此前父块展开后**不重排不截断**：多个命中子块被替换成同一段父块全文，
    # 同一篇长文可能进提示词 N 次（既浪费预算又挤掉其他文档的证据）。
    candidates: list[ContextItem] = []
    # key → 原始候选（保留 chunk 元信息，避免组装后再反查）
    meta: dict[str, tuple] = {}
    for rank, cand in enumerate(visible, start=1):
        chunk = cand.chunk
        filename = name_of(chunk.doc_id)
        score = cand.rerank_score if hybrid_result.reranked else cand.rrf_score
        content = chunk.content
        # 父块展开：用 parent_id 作为去重键，同一父块只保留最高分的那次命中
        dedupe_key = chunk.chunk_id
        if chunk.parent_id and parent_of is not None:
            parent = parent_of(chunk.parent_id)
            if parent is not None:
                content = parent.content
                dedupe_key = f"parent:{chunk.parent_id}"
        if dedupe_key in meta:
            continue  # 同一父块已收录：保留先出现（分数更高）的那次
        meta[dedupe_key] = (chunk, cand, f"{filename} · {chunk.source}")
        candidates.append(
            ContextItem(
                key=dedupe_key,
                doc_id=chunk.doc_id,
                content=content,
                score=float(score or 0.0),
                source=f"{filename} · {chunk.source}",
                rank=rank,
            )
        )
    return candidates, meta


def evidence_for_assembled(
    assembled,
    meta: dict[str, tuple],
    *,
    name_of: Callable[[str], str],
) -> list[Evidence]:
    """组装结果 → 已采纳证据（`used=True`），按原始 rank 重新编号。"""
    evidence: list[Evidence] = []
    for rank, item in enumerate(sorted(assembled.items, key=lambda c: c.rank), start=1):
        chunk, cand, source_label = meta[item.key]
        evidence.append(
            Evidence(
                rank=rank,
                chunk_id=chunk.chunk_id,
                score=round(item.score, 4) if item.score else None,
                content=item.content,
                source=source_label,
                doc_id=chunk.doc_id,
                filename=name_of(chunk.doc_id),
                source_route=cand.source_route,
                modality=chunk.modality,
                chunker=chunk.chunker,
                origin=cand.origin,
                used=True,
                # M4：文本证据关联的图像（前端回显原图）
                related_assets=list(getattr(chunk, "related_assets", []) or []),
                uri=asset_uri(chunk),
            )
        )
    return evidence


def rejected_evidence(
    hybrid_result: HybridResult,
    *,
    name_of: Callable[[str], str],
    start_rank: int = 1,
) -> list[Evidence]:
    """未采纳证据也必须返回（用户需要看到"检索到了但没用"），原因可归类。"""
    evidence: list[Evidence] = []
    for cand in hybrid_result.rejected:
        chunk = cand.chunk
        filename = name_of(chunk.doc_id)
        score = cand.rerank_score if hybrid_result.reranked else cand.rrf_score
        evidence.append(
            Evidence(
                rank=start_rank + len(evidence),
                chunk_id=chunk.chunk_id,
                score=round(score, 4) if score is not None else None,
                content=chunk.content,
                source=f"{filename} · {chunk.source}",
                doc_id=chunk.doc_id,
                filename=filename,
                source_route=cand.source_route,
                modality=chunk.modality,
                chunker=chunk.chunker,
                origin=cand.origin,
                used=False,
                # M4：文本证据关联的图像（前端回显原图）
                related_assets=list(getattr(chunk, "related_assets", []) or []),
                uri=asset_uri(chunk),
                rejected_reason="相关度分不足（未进 Top-K）",
            )
        )
    return evidence
