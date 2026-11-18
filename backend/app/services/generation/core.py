"""编排核心：阶段串联、事件产出、结果聚合、请求入口（`B0 X-27` 第二步）。

从 `services/pipeline.py` 抽出。这个模块的职责只有一件事：**按顺序调阶段、
把阶段结果拼成事件流**。它不实现任何阶段细节——缓存、检索、生成、自检都在
各自的 `*_stage` 模块里，这里只负责「什么时候调用它们、拿到结果怎么往下传」。

## 为什么首参是 `pip`

流水线是单例，`bind_principal()` 用 `__dict__.update` 产出请求级副本，
`_tenant_id` / `_experiment_label` / `_req_cache` / `_context_dropped`
都挂在副本上。编排必须作用在**副本**上，所以每个入口都接收 `pip`，
而不是把自己绑到某个协作者对象里——那会让所有请求共用单例的状态。

## 事件流是唯一事实

`ask()` 与 `ask_stream()` 共用 `core_events()`：前者把事件**聚合**成
`AskResult`，后者**逐条透传**。这样「阻塞返回的内容」与「流式返回的内容」
在结构上不可能漂移（此前是两份实现，`C-13` 才收敛）。
"""
import threading
import time
from typing import Iterator

from ...core.deadline import Deadline
from ...core.envelope import ApiError, ErrCode
from ...core.logging_setup import get_logger
from ...core.otel import span
from ...domain.constants import NO_INFO_ANSWER, REFUSAL_TEXT
from ...domain.schemas import AskResult, RetrievalConfig
from ..llm import LLMError
from ..retrieval.hybrid import HybridResult
from ..validation.crag import BRANCH_AMBIGUOUS, BRANCH_CORRECT, BRANCH_INCORRECT
from ..validation.history import CRAG_KIND
from ..validation.selfcheck import compute_confidence
from .prompt import (
    GENERAL_KNOWLEDGE_PROMPT,
    REASON_INSUFFICIENT,
    REASON_NO_CONTENT,
    build_evidence_block,
)
from .types import PipelineCancelled, route_degraded, route_model

logger = get_logger(__name__)


# ---- 指标 ----


def record(
    pip,
    question: str,
    tenant_id: str,
    ok: bool,
    latency_ms: float,
    from_cache: bool,
    llm_called: bool,
    path: str = "ask",
) -> None:
    pip._prom.requests_total.labels(status="ok" if ok else "error").inc()
    # label 由 tenant 改为 path（低基数）。按租户的延迟请看结构化日志。
    pip._prom.latency_seconds.labels(path=path).observe(latency_ms / 1000)
    pip._metrics_store.record_request(question, ok, latency_ms, from_cache, llm_called)
    # `ST-01` 阶段 A：上下文构成也进聚合（`/admin/metrics/context` 的数据源）
    if pip._context_breakdown:
        try:
            pip._metrics_store.record_context(pip._context_breakdown)
        except Exception:  # noqa: BLE001 - 指标失败不得影响问答
            pass


def core_events(
    pip, question: str, tenant_id: str, trace_id: str, config: RetrievalConfig,
    doc_id: str | None = None, deadline: "Deadline | None" = None,
    stages: dict[str, int] | None = None,
    stream_tokens: bool = False, cancel: "threading.Event | None" = None,
) -> Iterator[tuple[str, dict]]:
    """校验闭环主流程。产出的事件同时支撑 /ask（聚合）与 /ask/stream（透传）。

    `stream_tokens`（`C-13`）：**真流式**——生成阶段逐块产出 `token` 事件，
    调用方**立即外发**（不再等终稿后切片重放）。代价是**禁用重生成**
    （方案 A）：自检只下调置信度、不重写答案——否则「已发的 token」与
    「最终答案」会不一致（用户看到的与记录的不一样）。

    `cancel`（`C-04`）：客户端断开时置位；管线在**阶段边界**检查它并提前收尾。

    `deadline` 是请求级墙钟预算：传给每一次 LLM 调用，让上游超时与重试
    都受"剩余额度"约束（此前只有单次 60s 超时，整条链路没有上限）。

    `stages` 是**阶段耗时**的收集袋（`A-17`）：调用方传入，方法内写入
    `retrieval` / `rerank` / `llm` 等分段耗时，最终由调用方汇总成一行日志。
    """
    stages = stages if stages is not None else {}

    def _abort_stage(stage: str) -> None:
        """`C-04` 阶段边界检查：断了就别推进下一阶段。

        只在**阶段之间**检查（生成循环里已有逐块检查）。检索 / CRAG 精炼
        / 组装都可能是秒级同步调用，早一个阶段退出就能省掉整段上游成本；
        而 `reject_background_when_busy` 同时被占用的时间也一并缩短。
        """
        if cancel is not None and cancel.is_set():
            pip._prom.shed_total.labels(reason="cancel_stage").inc()
            logger.info("客户端断开，中止阶段 %s trace_id=%s", stage, trace_id)
            raise PipelineCancelled(stage)

    degrade = pip._degrade_level()
    # 生效的降级级别（独立指标，`C-10`）：开关默认关闭时恒为 0，
    # 打开后能直接看到「现在真的在降级，还是只是配了没触发」。
    pip._prom.degrade_level.set(degrade)
    if degrade >= 1:
        # 降级阶梯（批次 4，默认关）：上游慢/熔断时跳过预处理（意图/改写/拆解
        # 的多次 LLM 调用），用原查询直接检索——降级期保可用性，不保预处理质量。
        analysis = {"degraded": ["preprocess_skipped"], "llm_calls": 0}
        query_items = [("original", question)]
        is_chitchat = False
        pip._prom.shed_total.labels(reason="degrade_preprocess").inc()
        if degrade >= 2:
            # 级别 2（上游变慢）：连「额外改写」都不做——改写本身要调 LLM，
            # 上游正慢的时候再发这些请求只会更慢（并挤占许可）。
            analysis["degraded"].append("rewrite_skipped")
            pip._prom.shed_total.labels(reason="degrade_rewrite").inc()
    else:
        analysis, query_items, is_chitchat = pip._preprocess(question, tenant_id)
    preprocess_calls = (analysis or {}).get("llm_calls", 0)
    llm_calls = preprocess_calls
    if analysis is not None:
        yield "query_analysis", analysis

    # 阶段边界：预处理（意图/改写/拆解，多次 LLM 调用）结束后先看是否已断开。
    # 这里断开最值得早退——检索与 CRAG 都在后面，且都很贵。
    _abort_stage("preprocess")

    # 闲聊短路：直接回答，不校验
    if is_chitchat:
        answer = pip._llm_generate_or_degrade(
            "用户在闲聊。请友好、简洁地回应（一两句话），不要编造知识库内容。\n用户说：" + question,
            deadline=deadline,
        )
        if answer is None:
            # 上游不可用 → 降级模板（`C-12`）
            # 闲聊分支在检索之前，没有检索结果可带（None 即「无」）
            yield from pip._degraded_events(
                trace_id=trace_id, analysis=analysis, hybrid_result=None,
                crag_payload=None, llm_calls=llm_calls,
            )
            return
        llm_calls += 1
        # **不输出 `reranked`**：闲聊分支在检索之前，既没检索也就谈不上
        # 「精排 / 未精排」。此前硬编码 `True`，等于向外声明「已精排」；
        # 前端只在 `reranked === false` 时提示降级，省略字段正好两不误。
        yield "_final", {
            "answer": answer, "evidence": [], "analysis": analysis,
            "retrieval": None,
            "verdict": "answered", "confidence": 1.0,
            "llm_calls": llm_calls,
            "model_used": route_model(pip._llm),
            "routing_degraded": route_degraded(pip._llm),
        }
        return

    # ---- CRAG 循环（校验在生成之前全部完成）----
    rounds: list[dict] = []
    hybrid_result: HybridResult | None = None
    final_branch = "correct"
    unvalidated = False
    refusal_reason = REASON_NO_CONTENT
    # 降级时 CRAG 轮次收缩为 1（不精炼不重检索）
    max_rounds = 1 + (0 if degrade >= 1 else (pip._crag.max_iterations if pip._crag else 0))

    # 构造检索过滤条件并**下推**。此前 ACL 只在召回之后做后过滤，
    # 不可见文档会占满 top-k 名额（用户有权看 7 份，却因 3 份不可见
    # 排在前列而拿不到任何证据）。下推后 Milvus 在召回阶段就排除不可见文档，
    # FAISS 则由迭代扩取兜住。
    filters = pip._build_filters(tenant_id, doc_id)

    for round_no in range(1, max_rounds + 1):
        # 阶段边界：每轮检索前检查——CRAG 多轮时，第一轮断开就不该再检索。
        _abort_stage(f"crag_round_{round_no}")
        # 检索阶段 span（含查询数、最终召回的统计在下面挂属性）
        with span("retrieve", **{"kquery.queries": len(query_items),
                                 "kquery.round": round_no}) as sp:
            retrieve_started = time.perf_counter()
            try:
                hybrid_result = pip._hybrid.search_multi(
                    query_items, tenant_id, config, rerank_query=question, filters=filters
                )
            except ApiError:
                raise
            except Exception as exc:
                # 上游检索故障 → 统一的「检索失败」码 + 502（`A-10` 接线）。
                # 此前是裸异常冒泡 → 500「服务暂时不可用」：调用方分不清
                # 「向量库挂了（可重试）」与「服务整体坏了」。
                logger.exception("检索失败 trace_id=%s", trace_id)
                raise ApiError(
                    ErrCode.RETRIEVAL_FAILED,
                    "检索失败，请稍后重试",
                    http_status=502,
                ) from exc
            # 阶段耗时（`A-17`）：检索 = 召回 + 融合（精排单列，方便定位慢在哪一段）
            stats = hybrid_result.stats
            stages["rerank"] = int(getattr(stats, "rerank_ms", 0) or 0)
            stages["retrieval"] = max(
                0, int((time.perf_counter() - retrieve_started) * 1000) - stages["rerank"]
            )
            sp.set_attribute("kquery.candidates", len(hybrid_result.candidates))
            sp.set_attribute("kquery.reranked", bool(hybrid_result.reranked))
        pip._observe_retrieval(hybrid_result)
        # 阶段边界：检索与重排已经花掉了钱，断开后不必再跑 CRAG 评分/精炼。
        _abort_stage("retrieve")
        # 认证与授权已下线：不再有"检索后二次过滤"这一步，召回结果直接可用。
        candidates = list(hybrid_result.candidates)

        if pip._crag is None:
            break  # 未启用校验

        if not candidates:
            # 召回为空 → 知识库没有相关内容（拒答）
            rounds.append({"round": round_no, "branch": BRANCH_INCORRECT, "avg_score": 0.0, "evidence_count": 0, "refined_query": ""})
            pip._persist_round(trace_id, question, tenant_id, rounds[-1])
            yield "crag_branch", {"branch": BRANCH_INCORRECT, "avg_score": 0.0, "round": round_no}
            final_branch = BRANCH_INCORRECT
            refusal_reason = REASON_NO_CONTENT
            break

        # 证据评分：复用重排相关度分（与 同口径）；重排降级 → 评分不可用 → 降级不校验
        if not hybrid_result.reranked:
            unvalidated = True
            logger.warning("评分器降级（重排不可用）：跳过证据校验 trace_id=%s", trace_id)
            # 评分器不可用是**无感降级**：用户看不到 warn 提示（答案照常返回，
            # 只是没做证据校验），所以码在这里上报，不走信封（`A-10` 接线）。
            pip._prom.record_error(int(ErrCode.SCORER_UNAVAILABLE))
            break

        scores = sorted(
            (c.rerank_score for c in candidates if c.rerank_score is not None),
            reverse=True,
        )
        # Top-N 均值聚合：全量 Top-K 均值会被长尾无关候选稀释（配置项，随阈值一同标定）
        top_scores = scores[: pip._settings.crag_score_top_n]
        avg = sum(top_scores) / len(top_scores) if top_scores else 0.0
        branch = pip._crag.decide(avg)
        rounds.append({"round": round_no, "branch": branch, "avg_score": round(avg, 4), "evidence_count": len(candidates), "refined_query": ""})
        pip._persist_round(trace_id, question, tenant_id, rounds[-1])
        yield "crag_branch", {"branch": branch, "avg_score": round(avg, 4), "round": round_no}
        final_branch = branch

        if branch == BRANCH_CORRECT:
            break
        if branch == BRANCH_INCORRECT:
            break
        # ambiguous：精炼查询 → 重检索（改变提问角度，不是重复提问）
        if round_no < max_rounds:
            # 阶段边界：精炼本身要再调一次模型，断开后这轮重检索纯属浪费。
            _abort_stage("refine")
            hints = "；".join((c.chunk.content[:40] for c in candidates[:3]))
            refined = pip._crag.refine_query(pip._llm, question, hints)
            llm_calls += 1
            rounds[-1]["refined_query"] = refined
            yield "refine", {"before": question if round_no == 1 else rounds[-2].get("refined_query") or question, "after": refined}
            # **追加**而非替换。此前 `query_items = [("refined", refined)]`
            # 把 original + Fusion/HyDE/Step-Back 改写 + 全部子问题一次性丢弃 ——
            # 结果是"CRAG 认为证据不足 → 反而把召回面收窄到一条查询"，
            # 修复动作让情况更糟。正确语义是"在原召回面上再补一个角度"。
            existing = {q for _, q in query_items}
            if refined and refined not in existing:
                query_items = [*query_items, ("refined", refined)]

    crag_payload = {"branch": final_branch if pip._crag else "disabled", "rounds": rounds} if pip._crag else None

    # ---- 拒答判定（一等公民：走成功状态码）----
    # 通用知识兜底：无相关内容 / 证据耗尽时改由模型自身知识回答。
    if pip._crag is not None and final_branch == BRANCH_INCORRECT:
        if pip._settings.parametric_fallback_enabled:
            yield from pip._parametric_fallback_events(
                question, tenant_id, trace_id, analysis, hybrid_result,
                llm_calls, crag_payload, refusal_reason,
                deadline=deadline,
            )
            return
        yield "refused", {"reason": refusal_reason, "trace_id": trace_id}
        yield "_final", {
            "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
            "retrieval": hybrid_result.stats if hybrid_result else None,
            "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
            "verdict": "refused",
            "refusal_reason": refusal_reason, "confidence": 0.0,
            "crag": crag_payload, "llm_calls": llm_calls,
            "model_used": route_model(pip._llm),
            "routing_degraded": route_degraded(pip._llm),
        }
        return
    if pip._crag is not None and final_branch == BRANCH_AMBIGUOUS and len(rounds) >= max_rounds:
        if pip._settings.parametric_fallback_enabled:
            yield from pip._parametric_fallback_events(
                question, tenant_id, trace_id, analysis, hybrid_result,
                llm_calls, crag_payload, REASON_INSUFFICIENT,
                deadline=deadline,
            )
            return
        yield "refused", {"reason": REASON_INSUFFICIENT, "trace_id": trace_id}
        yield "_final", {
            "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
            "retrieval": hybrid_result.stats if hybrid_result else None,
            "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
            "verdict": "refused",
            "refusal_reason": REASON_INSUFFICIENT, "confidence": 0.0,
            "crag": crag_payload, "llm_calls": llm_calls,
            "model_used": route_model(pip._llm),
            "routing_degraded": route_degraded(pip._llm),
        }
        return

    # ---- 生成（证据已筛干净）----
    # 阶段边界：生成是整条链路上最贵的一步（上游按 token 计费），
    # 断开后不要开始。
    _abort_stage("generate")
    evidence = pip._to_evidence(hybrid_result)
    answer_source = "evidence"  # 通用知识兜底命中后改为 parametric
    used_evidence = [e for e in evidence if e.used]
    evidence_block = build_evidence_block(used_evidence)
    # 生成阶段 span（含证据条数；首 token 延迟由 runner 侧记录）
    with span("generate", **{"kquery.evidence": len(used_evidence)}) as sp:
        llm_started = time.perf_counter()
        if stream_tokens:
            # `C-13` 真流式：逐块外发，首 token 不再等整段生成完成。
            answer = ""
            for piece in pip._llm_stream(
                pip.build_prompt(question, used_evidence), deadline=deadline
            ):
                answer += piece
                yield "token", {"content": piece}
                # `C-04`：客户端断开 → 立刻停止生成（不等模型把话说完）
                if cancel is not None and cancel.is_set():
                    logger.info("客户端断开，中止生成 trace_id=%s", trace_id)
                    break
            stages["llm"] = int((time.perf_counter() - llm_started) * 1000)
            usage = getattr(pip._llm, "last_usage", None)
            if usage is not None:
                sp.set_attribute("gen_ai.usage.total_tokens", int(getattr(usage, "total", 0) or 0))
            llm_calls += 1
            yield from pip._finalize_streamed_answer(
                question, tenant_id, trace_id, analysis, hybrid_result,
                crag_payload, answer, used_evidence, evidence_block,
                llm_calls, stages, cancel, evidence=evidence,
            )
            return
        answer = pip._llm_generate_or_degrade(
            pip.build_prompt(question, used_evidence), deadline=deadline
        )
        if answer is None:
            # `C-12` 生成降级模板：上游不可用（熔断 / 超时 / 配额）时，
            # **不返回 5xx**，而是给一段可读的降级结果——
            # 用户看到的是「服务暂时不可用，请稍后重试」，
            # 而不是「Internal Server Error」或一个空答案。
            stages["llm"] = int((time.perf_counter() - llm_started) * 1000)
            yield from pip._degraded_events(
                trace_id=trace_id, analysis=analysis, hybrid_result=hybrid_result,
                crag_payload=crag_payload, llm_calls=llm_calls,
            )
            return
        stages["llm"] = int((time.perf_counter() - llm_started) * 1000)
        usage = getattr(pip._llm, "last_usage", None)
        if usage is not None:
            sp.set_attribute("gen_ai.usage.total_tokens", int(getattr(usage, "total", 0) or 0))
    llm_calls += 1

    # ---- 幻觉自检 + 受限重生成（只一次）----
    confidence: float | None = None
    regenerated = False
    unsupported_count = 0
    self_check_record = None
    if pip._self_checker is not None and used_evidence and pip._settings.selfcheck_enabled:
        check_started = time.perf_counter()
        try:
            check = pip._self_checker.check(answer, evidence_block)
            llm_calls += 1
            removed = 0
            recheck = check
            if check.is_hallucination and check.unsupported_claims:
                regenerated = True
                removed = len(check.unsupported_claims)
                answer = pip._self_checker.regenerate(question, evidence_block, check.unsupported_claims)
                llm_calls += 1
                # 二次校验：仍有无据声明 → 如实输出并下调置信度，不掩饰
                recheck = pip._self_checker.check(answer, evidence_block)
                llm_calls += 1
                unsupported_count = len(recheck.unsupported_claims)
            confidence = compute_confidence(unsupported_count)
            self_check_record = pip._to_self_check_record(
                pip._self_checker, recheck, answer, removed, unsupported_count
            )
            self_check_record.elapsed_ms = int((time.perf_counter() - check_started) * 1000)
            pip._persist_self_check(trace_id, tenant_id, self_check_record)
        except Exception as exc:
            # 4Axx 降级：自检不可用不阻断答案返回，置信度标为未知
            logger.warning("幻觉自检降级（本次未做答案校验）: %s", exc)
            # 同上：无感降级 → 码在这里上报（`A-10` 接线 4902）
            pip._prom.record_error(int(ErrCode.SELFCHECK_UNAVAILABLE))
            unvalidated = True
            confidence = None
    elif pip._settings.selfcheck_enabled and used_evidence:
        unvalidated = True  # 未配置自检器但启用校验 → 标未知

    # 提示词级拒答归一：LLM 判定证据不足时，verdict 统一为 refused（一等公民）
    prompt_level_refusal = answer.strip().startswith(NO_INFO_ANSWER)
    if prompt_level_refusal and pip._settings.parametric_fallback_enabled:
        # 通用知识兜底：模型判断证据不足 → 改由模型自身知识重答（仅一次）。
        # 自检已在上方跑过；兜底答案本就无据可核，不回自检/regenerate。
        fallback_answer = pip._llm_generate_or_degrade(
            GENERAL_KNOWLEDGE_PROMPT.format(question=question), deadline=deadline
        )
        if fallback_answer is None:
            # 上游不可用 → 降级模板（`C-12`）
            yield from pip._degraded_events(
                trace_id=trace_id, analysis=analysis, hybrid_result=hybrid_result,
                crag_payload=crag_payload, llm_calls=llm_calls,
            )
            return
        llm_calls += 1
        if not fallback_answer.strip().startswith(NO_INFO_ANSWER):
            answer = fallback_answer
            answer_source = "parametric"
            prompt_level_refusal = False
            confidence = round(float(pip._settings.parametric_fallback_confidence), 4)
            self_check_record = None
            unvalidated = True
            regenerated = False
            unsupported_count = 0
            evidence = []  # 兜底回答不引用证据，前端不展示无关片段
    if prompt_level_refusal:
        yield "refused", {"reason": REASON_NO_CONTENT, "trace_id": trace_id}
        yield "_final", {
            "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
            "retrieval": hybrid_result.stats if hybrid_result else None,
            "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
            "verdict": "refused", "refusal_reason": REASON_NO_CONTENT,
            "confidence": 0.0, "crag": crag_payload, "llm_calls": llm_calls,
            "model_used": route_model(pip._llm),
            "routing_degraded": route_degraded(pip._llm),
        }
        return

    verdict_out, refusal_reason_out = "answered", ""

    for ev in evidence:
        yield "evidence", ev.model_dump()
    if hybrid_result is not None:
        yield "retrieval_stats", {
            "from_cache": False,
            "reranked": hybrid_result.reranked,
            **hybrid_result.stats.model_dump(),
        }
    yield "confidence", {
        "verdict": verdict_out,
        "score": confidence,
        "unsupported_claims_count": unsupported_count,
        "regenerated": regenerated,
        "unvalidated": unvalidated,
        "answer_source": answer_source if verdict_out == "answered" else "",
        "self_check": self_check_record.model_dump() if self_check_record else None,
        "removed_claims": self_check_record.unsupported_claims if self_check_record else [],
    }
    # F5：推荐追问（失败不伤主答案）
    followups = pip._generate_followups(answer)
    yield "_final", {
        "answer": answer, "evidence": evidence if verdict_out == "answered" else [],
        "answer_source": answer_source if verdict_out == "answered" else "",
        "analysis": analysis,
        "retrieval": hybrid_result.stats if hybrid_result else None,
        "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
        "verdict": verdict_out, "refusal_reason": refusal_reason_out,
        "confidence": confidence,
        "regenerated": regenerated, "unsupported_claims_count": unsupported_count,
        "crag": crag_payload, "llm_calls": llm_calls,
        "self_check": self_check_record.model_dump() if self_check_record else None,
        "followups": followups,
        # 模型路由元信息（`LM-01`）：默认关时都是空/false
        "model_used": route_model(pip._llm),
        "routing_degraded": route_degraded(pip._llm),
        # 被丢弃的候选（`A-21.7`）：前端据此解释「为什么这条证据没进上下文」
        "context_dropped": list(pip._context_dropped),
        "context_breakdown": dict(pip._context_breakdown),
    }


def persist_round(pip, trace_id: str, question: str, tenant_id: str, round_data: dict) -> None:
    if pip._history is None:
        return
    pip._history.append(CRAG_KIND, {
        "trace_id": trace_id,
        "question": question,
        "tenant_id": tenant_id,
        **round_data,
    })


def log_stage_summary(trace_id: str, stages: dict[str, int], total_ms: int) -> None:
    """一行日志说清「慢在哪一段」。

    形如：`nodes={cache:2, retrieval:180, rerank:320, llm:1200} total=1853`。
    此前只能靠日志时间戳人工对齐四段（检索 / 重排 / 生成 / 自检），
    慢在哪一段要靠人肉比对——排障成本高到没人愿意查。
    """
    # 保留 0 毫秒的阶段：它表示「这一段走了，但没有耗时」，
    # 与「这一段没走」是两回事——过滤掉会让人以为缓存没生效。
    parts = ",".join(f"{k}:{v}" for k, v in stages.items())
    logger.info("nodes={%s} total=%d trace_id=%s", parts, total_ms, trace_id)


def assemble(pip, trace_id: str, started: float, events: list[tuple[str, dict]]) -> AskResult:
    final = next(payload for kind, payload in events if kind == "_final")
    elapsed = int((time.perf_counter() - started) * 1000)
    return AskResult(
        answer=final["answer"],
        evidence=final["evidence"],
        trace_id=trace_id,
        elapsed_ms=elapsed,
        from_cache=final.get("from_cache", False),
        reranked=final.get("reranked", True),
        retrieval=final.get("retrieval"),
        analysis=final.get("analysis"),
        verdict=final.get("verdict", "answered"),
        refusal_reason=final.get("refusal_reason", ""),
        answer_source=final.get("answer_source", ""),
        confidence=final.get("confidence"),
        regenerated=final.get("regenerated", False),
        unsupported_claims_count=final.get("unsupported_claims_count", 0),
        crag=final.get("crag"),
        self_check=final.get("self_check"),
        followups=final.get("followups") or [],
        context_breakdown=final.get("context_breakdown") or {},
        model_used=final.get("model_used") or "",
        routing_degraded=bool(final.get("routing_degraded")),
        context_dropped=final.get("context_dropped") or [],
    )


def record_quality(pip, result: AskResult) -> None:
    if hasattr(pip._metrics_store, "record_quality"):
        pip._metrics_store.record_quality(
            verdict=result.verdict,
            confidence=result.confidence,
            branch=(result.crag or {}).get("branch"),
            regenerated=result.regenerated,
            answer_source=result.answer_source,
        )


def cached_result(pip, entry, hit_kind: str, question: str, tenant_id: str,
                   trace_id: str, started: float) -> AskResult:
    pip._prom.cache_total.labels(result=hit_kind if hit_kind != "miss" else "hit").inc()
    elapsed = int((time.perf_counter() - started) * 1000)
    payload = {**entry.payload, "trace_id": trace_id, "elapsed_ms": elapsed, "from_cache": True}
    result = AskResult.model_validate(payload)
    pip._record(question, tenant_id, True, elapsed, from_cache=True, llm_called=False)
    logger.info("ask 命中缓存 trace_id=%s elapsed_ms=%d", trace_id, elapsed)
    return result


def ask(
    pip, question: str, tenant_id: str, trace_id: str, top_k: int | None = None,
    doc_id: str | None = None, deadline: "Deadline | None" = None,
    cancel: "threading.Event | None" = None,
) -> AskResult:
    """同步问答。

    `cancel`（`C-04`）：阻塞端点同样要能被打断——此前只有流式端点接线，
    客户端关掉页面后服务端仍会把检索/CRAG/生成跑完（白烧上游成本）。
    取消时抛 `PipelineCancelled`，由端点层静默收尾；半截结果**不写缓存**。
    """
    started = time.perf_counter()
    pip._reset_rcache()
    config = pip._scoped_config()
    if top_k is not None:
        config = config.model_copy(update={"top_k": top_k})
    digest, signature = pip._cache_dims(config)
    cache_key = pip._cache_key(question, tenant_id, config, doc_id)
    scope = doc_id or ""

    stages: dict[str, int] = {}
    cache_started = time.perf_counter()
    entry, hit_kind = pip._cache_lookup(
        question, tenant_id, cache_key, scope=scope, signature=signature
    )
    stages["cache"] = int((time.perf_counter() - cache_started) * 1000)
    if entry is not None:
        result = pip._cached_result(entry, hit_kind, question, tenant_id, trace_id, started)
        pip._log_stage_summary(trace_id, stages, result.elapsed_ms)
        return result

    ev, owner = pip._sf_acquire(cache_key)
    if not owner and ev.wait(timeout=pip._SF_WAIT_S):
        # 首个请求已算完并写入缓存：复用结果，不再打上游
        entry, hit_kind = pip._cache_lookup(question, tenant_id, cache_key, signature)
        if entry is not None:
            result = pip._cached_result(entry, hit_kind, question, tenant_id, trace_id, started)
            pip._log_stage_summary(trace_id, stages, result.elapsed_ms)
            return result

    pip._prom.cache_total.labels(result="miss").inc()
    try:
        events = list(
            pip._core_events(
                question, tenant_id, trace_id, config, doc_id,
                deadline=deadline, stages=stages, cancel=cancel,
            )
        )
        final = next(p for k, p in events if k == "_final")
        result = pip._assemble(trace_id, started, events)
        pip._cache_store(
            cache_key, question, result.model_dump(mode="json"), config.cache_ttl_s,
            tenant_id=tenant_id, scope=scope, signature=signature,

        )
        pip._prom.llm_calls_total.inc(final.get("llm_calls", 1))
        pip._record(question, tenant_id, True, result.elapsed_ms, from_cache=False, llm_called=True)
        pip._record_quality(result)
        logger.info(
            "ask 完成 trace_id=%s verdict=%s confidence=%s elapsed_ms=%d",
            trace_id, result.verdict, result.confidence, result.elapsed_ms,
        )
        pip._log_stage_summary(trace_id, stages, result.elapsed_ms)
        return result
    except PipelineCancelled:
        # 客户端已断开：**不是失败**（成功率不该被「用户走了」拉低），
        # 但半截计算也不该进缓存——之后同一个问题会拿到不完整的答案。
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("ask 因客户端断开中止 trace_id=%s elapsed_ms=%d", trace_id, elapsed)
        pip._log_stage_summary(trace_id, stages, elapsed)
        raise
    except LLMError:
        elapsed = int((time.perf_counter() - started) * 1000)
        pip._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=True)
        raise
    except Exception:
        elapsed = int((time.perf_counter() - started) * 1000)
        pip._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=False)
        raise
    finally:
        if owner:
            pip._sf_release(cache_key, ev)


def cached_stream_events(pip, entry, hit_kind: str, question: str, tenant_id: str,
                          trace_id: str, started: float) -> Iterator[tuple[str, dict]]:
    """缓存命中（含 single-flight 等待者）的事件重放。"""
    pip._prom.cache_total.labels(result=hit_kind if hit_kind != "miss" else "hit").inc()
    payload = entry.payload
    if payload.get("analysis"):
        yield "query_analysis", payload["analysis"]
    if payload.get("verdict") == "refused":
        yield "refused", {"reason": payload.get("refusal_reason", REASON_NO_CONTENT), "trace_id": trace_id}
    for ev in payload.get("evidence", []):
        yield "evidence", ev
    yield "retrieval_stats", {
        "from_cache": True,
        "hit_kind": hit_kind,
        "reranked": payload.get("reranked", True),
    }
    yield "confidence", {
        "verdict": payload.get("verdict", "answered"),
        "score": payload.get("confidence"),
        "unsupported_claims_count": payload.get("unsupported_claims_count", 0),
        "regenerated": payload.get("regenerated", False),
        "unvalidated": payload.get("confidence") is None,
        "crag": payload.get("crag"),
    }
    text = payload.get("answer", "")
    step = 12
    for i in range(0, len(text), step):
        yield "token", {"content": text[i : i + step]}
    # F5：推荐追问随答案一起缓存——缓存命中也必须能展示追问，
    # 否则"第二次问同一问题"就看不到追问按钮（用户会以为功能坏了）。
    cached_followups = payload.get("followups") or []
    if cached_followups:
        yield "followups", {"items": cached_followups}
    elapsed = int((time.perf_counter() - started) * 1000)
    pip._record(question, tenant_id, True, elapsed, from_cache=True, llm_called=False)
    yield "done", {"trace_id": trace_id, "elapsed_ms": elapsed, "from_cache": True,
                   "answer_source": payload.get("answer_source", "")}


def ask_stream(
    pip, question: str, tenant_id: str, trace_id: str, top_k: int | None = None,
    doc_id: str | None = None, deadline: "Deadline | None" = None,
    cancel: "threading.Event | None" = None,
) -> Iterator[tuple[str, dict]]:
    """流式问答（SSE 事件源）。

    `cancel`（`C-04`）：客户端断开时置位；管线在阶段边界与生成循环里检查它，
    **提前收尾**（不等模型把话说完）。
    """
    started = time.perf_counter()
    pip._reset_rcache()
    config = pip._scoped_config()
    if top_k is not None:
        config = config.model_copy(update={"top_k": top_k})
    digest, signature = pip._cache_dims(config)
    cache_key = pip._cache_key(question, tenant_id, config, doc_id)
    scope = doc_id or ""

    stages: dict[str, int] = {}
    cache_started = time.perf_counter()
    entry, hit_kind = pip._cache_lookup(
        question, tenant_id, cache_key, scope=scope, signature=signature
    )
    stages["cache"] = int((time.perf_counter() - cache_started) * 1000)
    if entry is not None:
        yield from pip._cached_stream_events(entry, hit_kind, question, tenant_id, trace_id, started)
        pip._log_stage_summary(trace_id, stages, int((time.perf_counter() - started) * 1000))
        return

    ev, owner = pip._sf_acquire(cache_key)
    if not owner and ev.wait(timeout=pip._SF_WAIT_S):
        entry, hit_kind = pip._cache_lookup(question, tenant_id, cache_key, signature)
        if entry is not None:
            yield from pip._cached_stream_events(entry, hit_kind, question, tenant_id, trace_id, started)
            return

    pip._prom.cache_total.labels(result="miss").inc()
    try:
        events: list[tuple[str, dict]] = []
        # `C-13`：**真流式**——token 在模型产出的瞬间就外发，
        # 不再等终稿后按 12 字切片重放（那样 TTFT = 全链路耗时）。
        # 代价是**禁重生成**（方案 A）：自检只下调置信度，不重写答案。
        # `C-04`：把取消事件传进管线，客户端断开时**在阶段边界**提前收尾。
        for kind, payload in pip._core_events(
            question, tenant_id, trace_id, config, doc_id,
            deadline=deadline, stages=stages,
            stream_tokens=True, cancel=cancel,
        ):
            events.append((kind, payload))
            if kind == "_final":
                # F5：推荐追问必须在 done 之前发出（前端白名单按事件名分发）
                followups = payload.get("followups") or []
                if followups:
                    yield "followups", {"items": followups}
                continue
            yield kind, payload

        result = pip._assemble(trace_id, started, events)
        pip._cache_store(
            cache_key, question, result.model_dump(mode="json"), config.cache_ttl_s,
            tenant_id=tenant_id, scope=scope, signature=signature,

        )
        final = next(p for k, p in events if k == "_final")
        pip._prom.llm_calls_total.inc(final.get("llm_calls", 1))
        pip._record(question, tenant_id, True, result.elapsed_ms, from_cache=False,
                     llm_called=True, path="ask_stream")
        pip._record_quality(result)
        logger.info(
            "ask_stream 完成 trace_id=%s verdict=%s confidence=%s elapsed_ms=%d",
            trace_id, result.verdict, result.confidence, result.elapsed_ms,
        )
        pip._log_stage_summary(trace_id, stages, result.elapsed_ms)
        yield "done", {"trace_id": trace_id, "elapsed_ms": result.elapsed_ms,
                       "from_cache": False, "answer_source": result.answer_source}
    except PipelineCancelled as exc:
        # 连接已断：不写 error 事件（写了也没人收），不计错误率，
        # 只留一条可诊断的阶段性日志；半截结果不进缓存（同 `ask`）。
        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info(
            "ask_stream 因客户端断开中止 trace_id=%s stage=%s elapsed_ms=%d",
            trace_id, exc.stage, elapsed,
        )
    except LLMError as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        pip._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=True, path="ask_stream")
        logger.warning("ask_stream 失败 trace_id=%s code=%s: %s", trace_id, exc.code, exc.message)
        yield "error", {"code": int(exc.code), "message": exc.message}
    except Exception:
        elapsed = int((time.perf_counter() - started) * 1000)
        pip._record(question, tenant_id, False, elapsed, from_cache=False, llm_called=False)
        logger.exception("ask_stream 异常 trace_id=%s", trace_id)
        yield "error", {"code": int(ErrCode.INTERNAL_ERROR), "message": "服务暂时不可用，请稍后再试"}
    finally:
        if owner:
            pip._sf_release(cache_key, ev)
