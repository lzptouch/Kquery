"""生成阶段：LLM 调用 / 降级 / 兜底 / 流式收尾 / 追问（`B0 X-27` 第二步）。

从 `services/pipeline.py` 抽出。这一段的特点是**只跟模型说话**：输入是提示词与
证据块，输出是 token 流或 `(kind, payload)` 事件；不碰缓存、不碰检索、不写路由。

三条兼容纪律原封不动搬过来（都是踩过坑才有的）：

1. `deadline` / `max_tokens` 是**可选能力**——适配器不支持就不传，而不是抛
   `TypeError` 让整次请求失败；
2. `stream()` 不支持时退回 `generate()`：真流式是增强，不是必需；
3. 只有**上游**故障才降级成可读文案，程序 bug 照旧抛出（`C-12`）。
"""
import threading
import time
from typing import Iterator

from ...core.deadline import Deadline, supports_deadline
from ...core.envelope import ErrCode
from ...core.logging_setup import get_logger
from ...domain.constants import (
    NO_INFO_ANSWER,
    REFUSAL_REASON_UPSTREAM_UNAVAILABLE,
    REFUSAL_TEXT,
    UPSTREAM_UNAVAILABLE_TEXT,
)
from ..validation.selfcheck import compute_confidence
from .prompt import GENERAL_KNOWLEDGE_PROMPT, REASON_NO_CONTENT
from .types import is_upstream_unavailable, route_degraded, route_model

logger = get_logger(__name__)


def llm_generate(
    pip,
    prompt: str,
    deadline: "Deadline | None" = None,
    max_tokens: int | None = None,
) -> str:
    """调用 LLM 生成。

    `deadline` 是**可选能力**：实现若不支持该关键字（第三方适配器、测试替身），
    就退化为无预算调用，而不是抛 TypeError 让整次请求失败。

    `max_tokens` 同理（`ST-04`）：实现不支持就不传（保持上游默认）。
    """
    kwargs: dict = {}
    if deadline is not None and supports_deadline(pip._llm.generate):
        kwargs["deadline"] = deadline
    if max_tokens:
        kwargs["max_tokens"] = int(max_tokens)
    if not kwargs:
        return pip._llm.generate(prompt)
    try:
        return pip._llm.generate(prompt, **kwargs)
    except TypeError:
        # 适配器不支持新关键字（第三方实现）→ 退回最小调用面
        return pip._llm.generate(prompt)


def llm_stream(pip, prompt: str, deadline: "Deadline | None" = None) -> Iterator[str]:
    """真流式调用（`C-13`）：逐块产出模型输出。

    **兼容纪律**：适配器不支持 `stream()`（第三方实现 / 测试替身）时
    退回 `generate()` —— 此时**仍是伪流式**（一次性产出），
    但**不报错**：真流式是增强，不是必需能力。
    """
    kwargs: dict = {}
    if deadline is not None and supports_deadline(pip._llm.stream):
        kwargs["deadline"] = deadline
    try:
        for piece in pip._llm.stream(prompt, **kwargs):
            if piece:
                yield str(piece)
        return
    except TypeError:
        # 适配器不支持 stream / deadline → 退回阻塞生成（一次性产出）
        # 走 `pip._llm_generate_or_degrade` 而非本模块函数：**保留替换点**——
        # 单测/集成测试用它注入上游故障，绕过方法就等于让降级路径失去覆盖。
        text = pip._llm_generate_or_degrade(prompt, deadline=deadline)
        if text:
            yield text
        return


def generate_or_degrade(pip, prompt: str, deadline: "Deadline | None" = None) -> str | None:
    """调用 LLM；**上游不可用**时返回 None（调用方转降级文案）。

    只有上游侧故障（熔断 / 超时 / 配额 / 依赖不可用）才降级——
    其它异常（程序 bug、参数错）照旧抛出，否则等于把 bug 藏进「稍后重试」。
    """
    try:
        # 走 `pip._llm_generate`（而不是本模块同名函数）：它是既有的替换点，
        # `test_degrade_generate_template` 等用 monkeypatch 打在这上面。
        return pip._llm_generate(prompt, deadline=deadline)
    except Exception as exc:
        if not is_upstream_unavailable(exc):
            raise
        logger.warning("上游不可用，转降级文案: %s", exc)
        pip._prom.shed_total.labels(reason="degrade_generate").inc()
        return None


def degraded_events(
    pip,
    *,
    trace_id: str,
    analysis: dict | None,
    hybrid_result,
    crag_payload,
    llm_calls: int,
) -> Iterator[tuple[str, dict]]:
    """生成降级模板的事件序列（`C-12`）：一段可读文案 + 拒答语义。"""
    yield "refused", {
        "reason": REFUSAL_REASON_UPSTREAM_UNAVAILABLE,
        "trace_id": trace_id,
        "degraded": True,
    }
    yield "_final", {
        "answer": UPSTREAM_UNAVAILABLE_TEXT,
        "evidence": [],
        "analysis": analysis,
        "retrieval": hybrid_result.stats if hybrid_result else None,
        "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
        "verdict": "refused",
        "refusal_reason": REFUSAL_REASON_UPSTREAM_UNAVAILABLE,
        "answer_source": "",
        "confidence": 0.0,
        "crag": crag_payload, "llm_calls": llm_calls,
        "self_check": None,
        "followups": [],
        "context_dropped": list(pip._context_dropped),
        "context_breakdown": dict(pip._context_breakdown),
        "model_used": route_model(pip._llm),
        "routing_degraded": route_degraded(pip._llm),
    }


def parametric_fallback_events(
    pip,
    question: str,
    tenant_id: str,
    trace_id: str,
    analysis: dict | None,
    hybrid_result,
    llm_calls: int,
    crag_payload: dict | None,
    refusal_reason: str,
    deadline: "Deadline | None" = None,
) -> Iterator[tuple[str, dict]]:
    """通用知识兜底（doc/analysis/通用知识兜底回答.md §6.7）。

    CRAG 判定「无相关内容/证据耗尽」时，改由模型自身知识回答：
    - 重生成仍输出拒答话术 → 维持原拒答（兜底失败不硬撑）；
    - 兜底成功 → verdict=answered + answer_source=parametric，不发 refused 事件。
    token 由调用方在 _final 后按终稿切块重放，本函数不直接产 token。
    """
    answer = pip._llm_generate_or_degrade(
        GENERAL_KNOWLEDGE_PROMPT.format(question=question), deadline=deadline
    )
    if answer is None:
        # 上游不可用 → 降级模板（`C-12`）：兜底失败也不该 5xx
        yield from degraded_events(
            pip,
            trace_id=trace_id, analysis=analysis, hybrid_result=hybrid_result,
            crag_payload=crag_payload, llm_calls=llm_calls,
        )
        return
    llm_calls += 1
    if answer.strip().startswith(NO_INFO_ANSWER):
        yield "refused", {"reason": refusal_reason, "trace_id": trace_id}
        yield "_final", {
            "answer": REFUSAL_TEXT, "evidence": [], "analysis": analysis,
            "retrieval": hybrid_result.stats if hybrid_result else None,
            "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
            "verdict": "refused", "refusal_reason": refusal_reason,
            "answer_source": "", "confidence": 0.0,
            "crag": crag_payload, "llm_calls": llm_calls,
            "model_used": route_model(pip._llm),
            "routing_degraded": route_degraded(pip._llm),
        }
        return
    confidence = round(float(pip._settings.parametric_fallback_confidence), 4)
    yield "confidence", {
        "verdict": "answered", "score": confidence,
        "unsupported_claims_count": 0, "regenerated": False,
        "unvalidated": True, "self_check": None, "removed_claims": [],
        "answer_source": "parametric",
    }
    yield "_final", {
        "answer": answer, "evidence": [], "analysis": analysis,
        "retrieval": hybrid_result.stats if hybrid_result else None,
        "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
        "verdict": "answered", "refusal_reason": "",
        "answer_source": "parametric", "confidence": confidence,
        "crag": crag_payload, "llm_calls": llm_calls,
        "model_used": route_model(pip._llm),
        "routing_degraded": route_degraded(pip._llm),
    }


def finalize_streamed_answer(
    pip,
    question: str,
    tenant_id: str,
    trace_id: str,
    analysis,
    hybrid_result,
    crag_payload,
    answer: str,
    used_evidence,
    evidence_block: str,
    llm_calls: int,
    stages: dict[str, int],
    cancel: "threading.Event | None" = None,
    evidence=None,
) -> Iterator[tuple[str, dict]]:
    """真流式的收尾（`C-13` 方案 A）：自检**不重写**，只下调置信度。

    ## 为什么禁重生成

    token 已经外发过了。若自检判定「需重写」并改写答案，
    **用户看到的**与**最终记录 / 缓存里的**就会不一致——
    这是比「残留无据声明」更糟的缺陷（记录不可信）。

    ## 代价如何呈现

    - 置信度**如实下调**（`unsupported_claims_count` / `confidence`）；
    - `regenerated=False` 且新增 `streamed_no_regen=True` 标记，
      前端可据此提示「本次为真流式，未做重生成」。
    """
    settings = pip._settings
    confidence: float | None = None
    unsupported_count = 0
    self_check_record = None
    unvalidated = False
    # 客户端已断开（`C-04` 断流探测 × `C-13` 流式收尾）：自检是**额外一次
    # 模型调用**，受众已走 → 跳过省成本与延迟；但必须**如实**标
    # `unvalidated`，不能让记录看起来「校验过」。
    # 此前 `cancel` 形参声明后函数体内无引用（死参数）。
    client_gone = cancel is not None and cancel.is_set()
    if client_gone:
        logger.info("流式收尾：客户端已断开，跳过自检（trace_id=%s）", trace_id)
        pip._prom.shed_total.labels(reason="cancel_selfcheck").inc()
        unvalidated = True
    elif pip._self_checker is not None and used_evidence and settings.selfcheck_enabled:
        check_started = time.perf_counter()
        try:
            check = pip._self_checker.check(answer, evidence_block)
            llm_calls += 1
            unsupported_count = len(check.unsupported_claims)
            confidence = compute_confidence(unsupported_count)
            self_check_record = pip._to_self_check_record(
                pip._self_checker, check, answer, 0, unsupported_count
            )
            self_check_record.elapsed_ms = int((time.perf_counter() - check_started) * 1000)
            pip._persist_self_check(trace_id, tenant_id, self_check_record)
        except Exception as exc:  # noqa: BLE001 - 自检失败不阻断答案
            logger.warning("幻觉自检降级（本次未做答案校验）: %s", exc)
            pip._prom.record_error(int(ErrCode.SELFCHECK_UNAVAILABLE))
            unvalidated = True
            confidence = None
    elif settings.selfcheck_enabled and used_evidence:
        unvalidated = True

    # 提示词级拒答归一：模型说「知识库无相关内容」→ 拒答（与阻塞路径同口径）
    prompt_level_refusal = answer.strip().startswith(NO_INFO_ANSWER)
    verdict_out, refusal_reason_out = "answered", ""
    if prompt_level_refusal:
        verdict_out, refusal_reason_out = "refused", REASON_NO_CONTENT

    # 证据与检索统计（与阻塞路径同口径：前端要展示引用来源）
    for ev in (evidence or []):
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
        "regenerated": False,
        "unvalidated": unvalidated,
        "answer_source": "" if verdict_out != "answered" else "evidence",
        "self_check": self_check_record.model_dump() if self_check_record else None,
        "removed_claims": [],
        # 真流式标记（`C-13`）：前端据此知道「本次没做重生成」
        "streamed_no_regen": True,
    }
    followups = pip._generate_followups(answer)
    yield "_final", {
        "answer": answer,
        "evidence": [ev.model_dump() for ev in (evidence or [])]
        if verdict_out == "answered" else [],
        "answer_source": "" if verdict_out != "answered" else "evidence",
        "analysis": analysis,
        "retrieval": hybrid_result.stats if hybrid_result else None,
        "reranked": bool(hybrid_result.reranked) if hybrid_result else False,
        "verdict": verdict_out, "refusal_reason": refusal_reason_out,
        "confidence": confidence,
        "regenerated": False,
        "unsupported_claims_count": unsupported_count,
        "crag": crag_payload, "llm_calls": llm_calls,
        "self_check": self_check_record.model_dump() if self_check_record else None,
        "followups": followups,
        "model_used": route_model(pip._llm),
        "routing_degraded": route_degraded(pip._llm),
        "context_dropped": list(pip._context_dropped),
        "context_breakdown": dict(pip._context_breakdown),
        # 真流式标记（`C-13`）：本次答案**未**经过重生成
        "streamed_no_regen": True,
    }


def generate_followups(pip, answer: str) -> list[str]:
    """生成推荐追问（**任何失败都吞掉**）。

    追问是增强项：LLM 不可用、超时、返回格式不对，都只该表现为
    "没有追问"，绝不能让已经生成好的答案失败。
    """
    if not answer or not answer.strip():
        return []
    try:
        generator = getattr(pip._llm, "generate_followups", None)
        if callable(generator):
            items = generator(answer, 3)
        else:
            # 按需载入：仅在特定路径用到
            from ..llm import generate_followups as _fallback

            items = _fallback(pip._llm, answer, 3)
    except Exception as exc:  # noqa: BLE001 - 追问失败不影响主答案
        logger.warning("生成推荐追问失败（本次不展示追问）: %s", exc)
        return []
    if not isinstance(items, list):
        return []
    cleaned = [str(i).strip() for i in items if str(i).strip()]
    return cleaned[:3]
