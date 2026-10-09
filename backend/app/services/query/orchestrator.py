"""Query 预处理编排器（阶段 4 §2.1 / §7.1）。

五步流水线：意图识别 → 查询改写 → 多跳拆解 → 数据源路由 → 检索执行（由流水线接管）。
每步失败都降级到最朴素路径并如实标注 degraded——整条链路上没有
"因为预处理失败而中断请求"的分支（本阶段最重要的流程约定）。

产出：
- QueryAnalysis 记录（可回放，含策略版本）；
- 检索用查询集合（原文 + 被真正使用的改写/子问题，预算截断）。
"""
import logging
import time
import uuid
from datetime import datetime, timezone

from app.services.query.decomposer import Decomposer, SourceRouter, mark_inactive_sources_skipped
from app.services.query.expanders import FusionExpander, HydeExpander, StepBackExpander
from app.services.query.intent import IntentClassifier
from app.services.query.jsonutil import JsonParseError
from app.services.query.schemas import (
    DECOMPOSE_INTENTS,
    ESTIMATED_TOKENS_PER_CALL,
    INTENT_TOKEN_PRICE_YUAN,
    REWRITE_ORIGINAL,
    IntentResult,
    QueryAnalysis,
    Rewrite,
    SubQuestion,
)

logger = logging.getLogger(__name__)


class QueryPreprocessor:
    def __init__(
        self,
        intent_classifier: IntentClassifier,
        expanders: list,  # Fusion/Hyde/StepBack 的启用组合（HyDE 与 Fusion 不同时启用）
        decomposer: Decomposer,
        router: SourceRouter,
        query_budget: int = 6,
        enabled: bool = True,
    ):
        self._intent = intent_classifier
        self._expanders = expanders
        self._decomposer = decomposer
        self._router = router
        self._budget = query_budget
        self._enabled = enabled

    def process(self, question: str, tenant_id: str) -> tuple[QueryAnalysis, list[tuple[str, str]]]:
        """返回 (分析记录, 检索查询集合)。查询集合每项为 (kind, text)，
        kind ∈ original / rewrite / hyde / stepback / sub_question（证据来源标注用）。"""
        started = time.perf_counter()
        analysis = QueryAnalysis(
            analysis_id=uuid.uuid4().hex[:16],
            raw_query=question,
            tenant_id=tenant_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        llm_calls = 0
        prompt_tokens = 0

        if not self._enabled:
            analysis.degraded.append("disabled")
            analysis.rewrites.append(Rewrite(type=REWRITE_ORIGINAL, text=question, expander="none"))
            analysis.sources = ["vector"]
            analysis.elapsed_ms = int((time.perf_counter() - started) * 1000)
            return analysis, [("original", question)]

        # ① 意图识别（失败 → 默认需要检索，46xx 静默降级）
        try:
            intent_result: IntentResult = self._intent.classify(question)
            llm_calls += 1
            analysis.intent = intent_result.intent
            analysis.intent_confidence = intent_result.confidence
            analysis.intent_reason = intent_result.reasoning
            analysis.needs_retrieval = intent_result.needs_retrieval
        except Exception as exc:
            logger.warning("意图识别降级（默认需要检索）: %s", exc)
            analysis.degraded.append("intent")
            analysis.needs_retrieval = True

        # 闲聊短路：直接 LLM 回答，不展示证据区
        if not analysis.needs_retrieval:
            analysis.rewrites.append(Rewrite(type=REWRITE_ORIGINAL, text=question, expander="none"))
            analysis.sources = []
            analysis.elapsed_ms = int((time.perf_counter() - started) * 1000)
            analysis.llm_calls = llm_calls
            return analysis, []

        # ② 查询改写（全部失败 → 用原 query，47xx 降级标注）
        rewrites = [Rewrite(type=REWRITE_ORIGINAL, text=question, expander="none")]
        expansion_ok = False
        for expander in self._expanders:
            try:
                produced = expander.expand(question)
                llm_calls += 1
                rewrites.extend(produced)
                expansion_ok = True
            except Exception as exc:
                logger.warning("改写器 %s 失败（标记未使用）: %s", expander.name, exc)
                llm_calls += 1
        if not expansion_ok and self._expanders:
            analysis.degraded.append("expansion")
        analysis.rewrites = rewrites

        # 检索查询集合：原文 + 被使用的改写（kind 随路传递为证据来源标注）
        query_items: list[tuple[str, str]] = [("original", question)]
        for rw in rewrites[1:]:
            if rw.used:
                query_items.append((rw.type, rw.text))

        # ③ 多跳拆解（仅 compare/summarize；失败 → 单跳，48xx 降级标注）
        if analysis.intent in DECOMPOSE_INTENTS:
            try:
                sub_questions = self._decomposer.decompose(question)
                llm_calls += 1
                sub_questions = mark_inactive_sources_skipped(sub_questions)
                analysis.sub_questions = sub_questions
                for sq in sub_questions:
                    query_items.append(("sub_question", sq.text))
            except Exception as exc:
                logger.warning("拆解降级（按单跳处理）: %s", exc)
                analysis.degraded.append("decompose")
                analysis.sub_questions = [SubQuestion(text=question, expected_source="vector")]

        # 预算截断：超出部分的改写标记"生成但未使用"
        if len(query_items) > self._budget:
            overflow = len(query_items) - self._budget
            query_items = query_items[: self._budget]
            marked = 0
            for rw in reversed(analysis.rewrites):
                if marked >= overflow:
                    break
                if rw.used and rw.type != REWRITE_ORIGINAL:
                    rw.used = False
                    rw.skip_reason = "预算截断（查询预算不足）"
                    marked += 1

        # ④ 数据源路由（兜底 vector）
        analysis.sources = self._router.route(question, analysis.intent, analysis.sub_questions)

        analysis.elapsed_ms = int((time.perf_counter() - started) * 1000)
        analysis.llm_calls = llm_calls
        # 费用估算：按"每次预处理调用约 600 输入 token + 200 输出 token"折算。
        # 拿不到真实 usage 时给估算值，比只给调用次数有用——"关掉 HyDE 省多少钱"
        # 才是调参时真正要回答的问题（stage-4 §7）。
        prompt_tokens = llm_calls * ESTIMATED_TOKENS_PER_CALL
        analysis.cost = round(prompt_tokens * INTENT_TOKEN_PRICE_YUAN, 6)
        logger.info(
            "预处理完成: intent=%s(%.2f) 改写=%d 子问题=%d 源=%s 降级=%s 耗时=%dms",
            analysis.intent, analysis.intent_confidence, len(analysis.rewrites),
            len(analysis.sub_questions), analysis.sources, analysis.degraded, analysis.elapsed_ms,
        )
        return analysis, query_items
