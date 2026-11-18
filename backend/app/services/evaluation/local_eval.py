"""本地检索评测（零外部依赖）：装配检索栈 → 跑查询 → 打分。

**口径**（与 `scripts/eval/run_eval.py --mode local` 一致）：
- 关键词路 = **真 BM25**（与生产同一实现）；
- 语义路 = **mock embedding**（特征哈希词袋，确定性可复现，但绝对召回不代表线上）；
- 融合 / 精排 = 与生产同一实现（RRF k=60 / 候选池 20 / 重排）。

所以它适合回答"**分路贡献多少、并集上限在哪、缺口卡在哪一环**"，
不适合回答"线上 recall 是多少"（后者要用 `--mode live`）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.services.evaluation import metrics as M


def build_retriever(chunks: list, dim: int = 1024, embeddings=None):
    """装配双路检索器（BM25 + 向量）+ RRF 融合 + 重排。

    `debug_routes=True`：保留各路原始候选，供分路 recall / 并集上限 / 缺口归因。
    `rejected_limit=500`：让"融合候选池"可完整还原（默认 3 条不够做归因）。
    """
    # 按需载入：仅在特定路径用到
    from app.services.embeddings import MockEmbeddings
    # 按需载入：仅在特定路径用到
    from app.services.retrieval.bm25 import BM25Retriever
    # 按需载入：仅在特定路径用到
    from app.services.retrieval.hybrid import HybridRetriever
    # 按需载入：仅在特定路径用到
    from app.services.retrieval.rerank import MockReranker
    # 按需载入：仅在特定路径用到
    from app.services.retrieval.vector import VectorRetriever
    # 按需载入：仅在特定路径用到
    from app.services.vectorstore import VectorStore

    embeddings = embeddings or MockEmbeddings(dim=dim)
    bm25 = BM25Retriever()
    bm25.add(chunks)
    tmp = tempfile.TemporaryDirectory()
    store = VectorStore(Path(tmp.name), dim)
    store.add(chunks, embeddings.embed_texts([c.content for c in chunks]))
    hybrid = HybridRetriever(
        {"keyword": bm25, "vector": VectorRetriever(embeddings, store)},
        MockReranker(),
        debug_routes=True,
        rejected_limit=500,
    )
    # TemporaryDirectory 必须活到检索结束：挂到 retriever 上避免被 GC 回收
    hybrid._eval_tmpdir = tmp  # type: ignore[attr-defined]
    return hybrid


def run_queries(
    retriever,
    queries: list[dict],
    tenant_id: str = "eval-tenant",
    top_k: int = 10,
    config=None,
) -> dict[str, dict]:
    """逐条查询 → 检索结果快照（``{query_id: {ranked, routes, fused}}``）。"""
    # 按需载入：仅在特定路径用到
    from app.domain.schemas import RetrievalConfig

    config = config or RetrievalConfig()
    runs: dict[str, dict] = {}
    for q in queries:
        qid = str(q["query_id"])
        result = retriever.search(
            str(q.get("question") or ""), tenant_id, config, top_k=top_k
        )
        ranked = [c.chunk.chunk_id for c in result.candidates]
        routes = {
            name: [d.get("chunk_id", "") for d in (items or [])]
            for name, items in (result.debug_routes or {}).items()
        }
        fused = ranked + [c.chunk.chunk_id for c in result.rejected]
        runs[qid] = {"query_id": qid, "ranked": ranked, "routes": routes, "fused": fused}
    return runs


def score_runs(
    runs: dict[str, dict],
    qrels: dict[str, dict[str, int]],
    k_values=(5, 10, 20),
    kinds: dict[str, str] | None = None,
    answers: dict[str, str] | None = None,
    chunk_text: dict[str, str] | None = None,
) -> dict:
    """打分 + 汇总 + 缺口归因（qrels 必须是**已解析的** chunk_id 形式）。

    `kinds`：query_id → 题型（"text" / "vision"）。用于**分组统计**：
    "必须看图"的题（答案不在图注里）单独成组，它与文本题组的差值
    就是**跨模态能力的缺口**——文本路在这组题上原理上答不出。
    """
    per_query: list[dict] = []
    attributions: list[dict] = []
    details: dict[str, dict] = {}
    for qid, grades in qrels.items():
        row = runs.get(qid) or {}
        ranked = list(row.get("ranked") or [])
        res = M.evaluate_query(ranked, grades, k_values=k_values)
        if kinds:
            res["kind"] = kinds.get(qid, "text")
        # 文本可答性：检索到的**文本**里有没有答案关键词。
        # 0 = 这道题必须看原图（检索到了图，但文字答不出）→ 跨模态缺口。
        key = (answers or {}).get(qid, "")
        if key and chunk_text:
            # 只看**该题的目标块**（这题的答案块 = 那张图的图注），
            # 不看检索到的全部文本：其他图的图注里出现同一个词，
            # 不代表"这道题能从文字里答出来"。
            got = " ".join(chunk_text.get(cid, "") for cid in grades)
            res["text_answerable"] = bool(key and key in got)
        routes = row.get("routes") or {}
        rel = M.relevant_ids(grades)
        res["routes"] = (
            {str(k): M.route_recall(routes, rel, k) for k in k_values} if routes else {}
        )
        # 天花板 = 候选池口径（各路完整候选的并集），不是 @k
        res["union_ceiling"] = round(M.union_ceiling(routes, rel), 4) if routes else None
        if routes:
            attributions.append(
                M.gap_attribution(routes, list(row.get("fused") or []), ranked, rel)
            )
        per_query.append(res)
        details[qid] = res
    return {
        "aggregate": M.aggregate(per_query),
        "attribution": M.aggregate_attribution(attributions),
        "per_query": details,
    }
