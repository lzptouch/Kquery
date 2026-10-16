"""混合检索编排（阶段 2 README §2.1 四段式链路）。

BM25 取前 20 + 向量取前 20 → RRF 融合取前 20 → gte-rerank-v2 精排取 Top-K。
多召是为了给 RRF 与 Rerank 留候选空间；重排只对统一候选集做一次精排。

降级语义：重排失败 → 跳过重排使用融合结果，reranked=False 显式标记，
绝不让重排阻断整次检索。
"""
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.domain.schemas import RetrievalConfig, RetrievalStats
from app.services.retrieval.base import FusedCandidate, Retriever
from app.services.retrieval.fusion import rrf_fuse
from app.services.retrieval.rerank import RerankError, Reranker
from app.services.vectorstore import is_current_version

logger = logging.getLogger(__name__)


class RetrievalError(Exception):
    """检索整体失败（**全部**检索路都挂了才抛）。

    单路失败在 search_multi 内已降级（record 到 `stats.degraded_routes` 并继续），
    只有一路都没成功时才是真的失败，由上层 CRAG/拒答逻辑处理（TASK-147）。
    """


def _rerank(reranker, query: str, candidates, top_n: int, model: str) -> list[FusedCandidate]:
    """调用重排器。

    TASK-159：删除原先的 `inspect.signature` 签名探测。
    那种写法把"接口契约"变成了"运行时猜谜"：参数名恰好叫 `model` 就传、
    否则不传；换实现、加装饰器、用 C 扩展都会让探测结果漂移，且**不会有报错**，
    只会静默走错分支。`Reranker` 是项目内 Protocol，`model` 已是正式参数
    （`rerank.py` 的 QwenReranker 与 MockReranker 都实现了它），直接传即可。
    """
    return reranker.rerank(query, candidates, top_n, model=model)


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
        # 索引版本查询（DEF-R-2 / P0-7）：doc_id → 当前版本号。
        # 召回候选按此过滤历史版本块，否则 reparse 后新旧版本同时被召回污染结果。
        version_of: Callable[[str], int] | None = None,
    ):
        self._retrievers = retrievers
        self._reranker = reranker
        self._per_route_top_n = per_route_top_n
        self._fused_top_n = fused_top_n
        self._rrf_k = rrf_k
        self._version_of = version_of
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
        filters=None,
    ) -> HybridResult:
        return self.search_multi(
            [("original", query)], tenant_id, config, rerank_query=query,
            doc_id=doc_id, filters=filters,
        )

    def search_multi(
        self,
        queries: list[tuple[str, str]],
        tenant_id: str,
        config: RetrievalConfig,
        rerank_query: str,
        doc_id: str | None = None,
        filters=None,
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
        degraded_routes: list[str] = []
        for kind, text in queries:
            origin = {"kind": kind, "text": text}
            for route_name, prefix in enabled:
                started = time.perf_counter()
                # TASK-147：**单路失败必须降级而非拖垮整次检索**。
                # 此前的写法是裸调用：embedding 服务抖一下 → 向量路抛错 →
                # 整个 search_multi 失败 → BM25 已经拿到的结果一并丢弃。
                # 这与本项目在 CRAG / 重排 / 幻觉自检上贯彻的降级纪律完全不一致。
                try:
                    # TASK-142：把 ACL 允许清单与版本条件一并下推给各后端。
                    # 后端不支持表达式时（FAISS）由它自己做迭代扩取兜住名额损耗。
                    candidates = self._retrievers[route_name].search(
                        text, per_route_top_n, tenant_id, doc_id=doc_id, filters=filters
                    )
                except Exception as exc:  # noqa: BLE001 - 任一路失败都不应中断检索
                    logger.warning("检索路 %s 失败（已降级，本次跳过该路）: %s", route_name, exc)
                    if route_name not in degraded_routes:
                        degraded_routes.append(route_name)
                    continue
                ms = int((time.perf_counter() - started) * 1000)
                setattr(stats, f"{prefix}_ms", getattr(stats, f"{prefix}_ms") + ms)
                setattr(stats, f"{prefix}_candidates", getattr(stats, f"{prefix}_candidates") + len(candidates))
                route_results.append((f"{route_name}@{kind}", candidates, origin))

        # 全部路都挂掉才算真失败；部分降级要在 stats 里可见（否则"少召回"无从解释）
        if degraded_routes and not route_results:
            raise RetrievalError(f"全部检索路失败: {', '.join(degraded_routes)}")
        if degraded_routes:
            stats.degraded_routes = degraded_routes

        if not route_results:
            return HybridResult(candidates=[], stats=stats, reranked=False)

        # ①.5 索引版本过滤（DEF-R-2 / P0-7）：剔除历史版本块，只保留各文档当前版本。
        # 在融合前统一过滤（而非三个召回后端各自下推），保证 FAISS/Milvus/BM25 行为一致。
        if self._version_of is not None:
            route_results = [
                (
                    route,
                    [
                        c for c in cands
                        if is_current_version(c.chunk.chunk_id, c.chunk.doc_id, self._version_of(c.chunk.doc_id))
                    ],
                    origin,
                )
                for route, cands, origin in route_results
            ]
            route_results = [r for r in route_results if r[1]]

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
