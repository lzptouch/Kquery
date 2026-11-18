"""检索阶段：预处理 / 过滤条件 / 证据装配 / 检索观测（`B0 X-27` 第二步）。

从 `services/pipeline.py` 抽出。判据是「这一段的输入只有问题与检索结果，
输出只有证据与观测值」——不需要知道 LLM、缓存、自检的任何细节。

首参 `pip` 是请求级副本：`_to_evidence` 要往副本上写
`_context_dropped` / `_context_breakdown`（给前端解释「这条为什么没进上下文」），
写副本正是现有语义（`bind_principal` 让每个请求有自己的那份）。
"""
from ...core.logging_setup import get_logger
from ...domain.schemas import Evidence
from ..context import assemble_context
from ..retrieval.filters import RetrievalFilters
from ..retrieval.hybrid import HybridResult
from .evidence import build_candidates, evidence_for_assembled, rejected_evidence

logger = get_logger(__name__)


def degrade_level(pip) -> int:
    """降级阶梯水位（默认 0=正常）。

    | 级别 | 判据 | 动作 |
    |---|---|---|
    | 1 | 上游**熔断打开** | 收缩预处理与 CRAG 轮次 |
    | 2 | 上游**耗时 P95 > 阈值**（`C-10`） | 同上，并额外关掉 HyDE / 额外改写 |

    级别 2 是「事前」判据：熔断是事后（已经失败了 N 次才打开），
    P95 是事前（上游正在变慢，但还没失败）——后者才是保住可用性的关键。

    **默认关闭**（`degrade_ladder_enabled`）：阈值必须先标定，
    否则会误降级（正常单次 LLM 调用就 3~8s，阈值定 2000ms 会一直降级）。
    """
    if not getattr(pip._settings, "degrade_ladder_enabled", False):
        return 0
    try:
        # 按需载入：仅在特定路径用到
        from ..upstream import bulkhead

        if bulkhead("rerank").stats().get("state") == "open":
            return 1
    except Exception:  # noqa: BLE001 - 观测失败不降级
        pass
    # `C-10`：上游耗时 P95 超阈值 → 级别 2。
    # 样本不足（<5）时不据此降级：刚启动时窗口是空的，
    # 此时「P95=0」会被误读成「上游很快」。
    try:
        # 按需载入：仅在特定路径用到
        from ..upstream import bulkhead

        threshold_ms = float(getattr(pip._settings, "degrade_upstream_slow_ms", 0) or 0)
        if threshold_ms > 0:
            for name in ("llm", "embedding", "rerank"):
                st = bulkhead(name).stats()
                if st.get("latency_samples", 0) >= 5 and st.get("p95_ms", 0.0) > threshold_ms:
                    logger.warning(
                        "上游 %s 耗时 P95=%.0fms > 阈值 %.0fms：进入降级级别 2",
                        name, st.get("p95_ms", 0.0), threshold_ms,
                    )
                    return 2
    except Exception:  # noqa: BLE001 - 观测失败不降级
        pass
    return 0


def preprocess(pip, question: str, tenant_id: str) -> tuple[dict | None, list[tuple[str, str]], bool]:
    """问题预处理。返回 (分析结果 dump, [(kind, query)], 是否跳过检索)。"""
    if pip._preprocessor is None:
        return None, [("original", question)], False
    analysis_obj, query_items = pip._preprocessor.process(question, tenant_id)
    if pip._analysis_store is not None:
        pip._analysis_store.save(analysis_obj)
    return analysis_obj.model_dump(), query_items, not analysis_obj.needs_retrieval


def build_filters(pip, tenant_id: str, doc_id: str | None = None):
    """构造检索过滤条件（F4 快问）。

    认证与授权已下线后**不再有文档级可见性过滤**：唯一保留的是
    `doc_id`（快问单文档范围）与 `tenant_id`（数据隔离维度）。

    **F4 关键修正（保留）**：`doc_id` 必须独立生效——否则"只检索这一个文件"
    的要求会被整个丢掉，快问会检索全库，答案里混进无关文档的引用。
    """
    if doc_id:
        return RetrievalFilters(tenant_id=tenant_id, doc_id=doc_id)
    return None


def to_evidence(pip, hybrid_result: HybridResult) -> list[Evidence]:
    """装配证据。

    认证与授权已下线：不再有"检索后二次校验"这一步（此前是文档级 ACL 的
    应用层一侧），召回结果直接进入证据装配。

    装配逻辑在 `services/generation/evidence.py`（纯函数，依赖用回调传入）；
    这里只做「读设置 → 调纯函数 → 收集观测值」。
    """
    settings = pip._settings
    candidates, meta = build_candidates(
        hybrid_result,
        name_of=pip._name_of,
        parent_of=pip._parent_of if pip._docstore is not None else None,
    )

    assembled = assemble_context(
        candidates,
        token_budget=int(getattr(settings, "context_token_budget", 0) or 0),
        mmr_lambda=float(getattr(settings, "context_mmr_lambda", 1.0) or 1.0),
    )
    if assembled.dropped:
        logger.info(
            "上下文组装：%d 条候选 → %d 条（约 %d token，丢弃 %d 条）",
            len(candidates), len(assembled.items), assembled.tokens, len(assembled.dropped),
        )
    # 供前端解释"为什么这条没进上下文"
    pip._context_dropped = [
        {"key": d.key, "reason": d.reason, "doc_id": d.doc_id} for d in assembled.dropped
    ]
    # `ST-01` 阶段 A：构造上下文信封（**只观测**——渲染仍走既有路径，逐字不变）。
    # 构成表进响应：五个来源的条数 / token / 配额 + 被丢弃条目。
    pip._context_breakdown = build_context_breakdown(pip, assembled)

    evidence = evidence_for_assembled(assembled, meta, name_of=pip._name_of)
    # 未采纳证据也必须返回（用户需要看到"检索到了但没用"），原因可归类
    evidence.extend(
        rejected_evidence(hybrid_result, name_of=pip._name_of, start_rank=len(evidence) + 1)
    )
    return evidence


def build_context_breakdown(pip, assembled) -> dict:
    """构造上下文构成表（`ST-01` 阶段 A）。

    **只观测**：不改变任何渲染输出，只回答「这次上下文里有什么、各占多少」。
    构成表 = 分区器给出的来源视图 + 装配阶段被丢弃的条目。
    """
    # 按需载入：只有装配之后才需要分区器（纯测量路径，不必进热路径）
    from ..context import ContextItem, build_budget, partition

    budget = build_budget(int(getattr(pip._settings, "context_token_budget", 0) or 0))
    envelope = partition(
        budget=budget,
        evidence=[
            ContextItem(
                key=item.key, doc_id=item.doc_id, content=item.content,
                score=item.score, source=item.source, rank=item.rank,
            )
            for item in assembled.items
        ],
    )
    breakdown = envelope.breakdown()
    # 装配阶段被丢弃的条目（此前**没有任何消费点**——`ST-01` 约束 2）
    dropped = [
        {"source": "evidence", "key": d.key, "tokens": 0, "reason": d.reason}
        for d in assembled.dropped
    ]
    breakdown["dropped"] = breakdown.get("dropped", []) + dropped
    breakdown["dropped_count"] = len(breakdown["dropped"])
    return breakdown


def observe_retrieval(pip, hybrid_result: HybridResult) -> None:
    """把检索各阶段耗时与首条分数记进指标。"""
    stats = hybrid_result.stats
    for stage in ("keyword", "vector", "fusion", "rerank"):
        ms = getattr(stats, f"{stage}_ms", 0)
        if ms:
            pip._prom.retrieval_stage_ms.labels(stage=stage).observe(ms)
    if hybrid_result.candidates:
        top = hybrid_result.candidates[0]
        pip._prom.retrieval_top_score.observe(top.rerank_score or top.rrf_score)
