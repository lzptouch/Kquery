"""混合检索编排（阶段 2 README §2.1 四段式链路）。

BM25 取前 20 + 向量取前 20 → RRF 融合取前 20 → gte-rerank-v2 精排取 Top-K。
多召是为了给 RRF 与 Rerank 留候选空间；重排只对统一候选集做一次精排。

降级语义：重排失败 → 跳过重排使用融合结果，reranked=False 显式标记，
绝不让重排阻断整次检索。
"""
import inspect
import logging
import time
from dataclasses import dataclass, field

from app.domain.schemas import RetrievalConfig, RetrievalStats
from app.services.retrieval.base import FusedCandidate, Retriever
from app.services.retrieval.fusion import rrf_fuse
from app.services.retrieval.rerank import RerankError, Reranker

logger = logging.getLogger(__name__)


def _rerank(reranker, query: str, candidates, top_n: int, model: str) -> list[FusedCandidate]:
    """调用重排器，按签名决定是否传 `model`。

    新增热更新参数后若直接硬传 `model=`，所有既有 `Reranker` 实现
    （含测试替身与第三方实现）都会因签名不匹配而直接崩——把"可选能力"
    实现成"必选契约"是典型的破坏性变更。这里做签名探测后优雅降级。
    """
    try:
        params = inspect.signature(reranker.rerank).parameters
    except (TypeError, ValueError):  # 内建/C 实现无法探测
        params = {}
    if "model" in params or any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return reranker.rerank(query, candidates, top_n, model=model)
    return reranker.rerank(query, candidates, top_n)


@dataclass
class HybridResult:
    candidates: list[FusedCandidate]  # 最终进入上下文的候选（已按相关度排序）
    stats: RetrievalStats
    reranked: bool  # False = 重排降级，本次未精排
    # 阶段 5：未采纳的候选（召回但未进 Top-K），随响应返回供用户判断有无遗漏
    rejected: list[FusedCandidate] = field(default_factory=list)


class HybridRetriever:
    def __init__(
        self,
        retrievers: dict[str, Retriever],
        reranker: Reranker,
        per_route_top_n: int = 20,
        fused_top_n: int = 20,
        rrf_k: int = 60,
        settings=None,
    ):
        self._retrievers = retrievers
        self._reranker = reranker
        self._per_route_top_n = per_route_top_n
        self._fused_top_n = fused_top_n
        self._rrf_k = rrf_k
        # 构造参数只是"没有运行时配置时的兜底"；实际生效值以 RetrievalConfig 为准，
        # 这样 /admin/retrieval-config 改 RRF 常数与两级召回数才能真正生效
        self._settings = settings

    def _limits(self, config: RetrievalConfig) -> tuple[int, int, int]:
        """召回条数 / 融合候选数 / RRF 常数：运行时配置优先，零值回落构造默认。"""
        per_route = config.per_route_top_n or self._per_route_top_n
        fused = config.fused_top_n or self._fused_top_n
        rrf_k = config.rrf_k or self._rrf_k
        return max(1, per_route), max(1, fused), max(1, rrf_k)

    def search(
        self,
        query: str,
        tenant_id: str,
        config: RetrievalConfig,
        doc_id: str | None = None,
    ) -> HybridResult:
        return self.search_multi(
            [("original", query)], tenant_id, config, rerank_query=query, doc_id=doc_id
        )

    def search_multi(
        self,
        queries: list[tuple[str, str]],
        tenant_id: str,
        config: RetrievalConfig,
        rerank_query: str,
        doc_id: str | None = None,
    ) -> HybridResult:
        """多查询混合检索（阶段 4）：每条查询（原文/改写/子问题）分别双路召回，
        全部路结果进同一 RRF 融合（来源标注随路传递），重排以用户原话为基准。"""
        stats = RetrievalStats()
        per_route_top_n, fused_top_n, rrf_k = self._limits(config)

        # ① 双路召回（按开关选择启用路）× 每条查询
        route_results: list[tuple] = []
        enabled: list[tuple[str, str]] = []  # (route_name, stats 前缀)
        if config.bm25_enabled and "keyword" in self._retrievers:
            enabled.append(("keyword", "keyword"))
        if config.vector_enabled and "vector" in self._retrievers:
            enabled.append(("vector", "vector"))
        for kind, text in queries:
            origin = {"kind": kind, "text": text}
            for route_name, prefix in enabled:
                started = time.perf_counter()
                candidates = self._retrievers[route_name].search(
                    text, per_route_top_n, tenant_id, doc_id=doc_id
                )
                ms = int((time.perf_counter() - started) * 1000)
                setattr(stats, f"{prefix}_ms", getattr(stats, f"{prefix}_ms") + ms)
                setattr(stats, f"{prefix}_candidates", getattr(stats, f"{prefix}_candidates") + len(candidates))
                route_results.append((f"{route_name}@{kind}", candidates, origin))

        if not route_results:
            return HybridResult(candidates=[], stats=stats, reranked=False)

        # ② RRF 融合（只用排名不用分数，跨路比较的唯一合法位置）
        started = time.perf_counter()
        weights = {"keyword": config.bm25_weight, "vector": config.vector_weight}
        fused = rrf_fuse(route_results, k=rrf_k, weights=weights, top_n=fused_top_n)
        stats.fusion_ms = int((time.perf_counter() - started) * 1000)
        stats.fused_count = len(fused)

        # ③ 重排精排（增强能力：失败降级为融合序；以用户原话为基准）
        reranked = False
        if config.rerank_enabled and fused:
            started = time.perf_counter()
            try:
                final = _rerank(
                    self._reranker, rerank_query, fused, config.top_k, config.rerank_model
                )
                stats.rerank_ms = int((time.perf_counter() - started) * 1000)
                stats.reranked_count = len(final)
                reranked = True
            except RerankError as exc:
                stats.rerank_ms = int((time.perf_counter() - started) * 1000)
                logger.warning("重排降级（本次未精排）: %s", exc)
                final = fused[: config.top_k]
                stats.reranked_count = 0
        else:
            final = fused[: config.top_k]
            stats.reranked_count = 0

        stats.into_context_count = len(final)
        # 未采纳候选：在融合集但未进最终 Top-K（取前 3 条展示，原因可归类）
        final_ids = {c.chunk.chunk_id for c in final}
        rejected = [c for c in fused if c.chunk.chunk_id not in final_ids][:3]
        return HybridResult(candidates=final, stats=stats, reranked=reranked, rejected=rejected)
