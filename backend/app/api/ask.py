"""问答端点（阶段 1 §4 + 阶段 6 Agentic + 阶段 9 安全网关）。

网关顺序（§10.1）：认证 → Principal → 配额（滑动窗口）→ Agent 执行
（检索前租户过滤 + 检索后 ACL）→ 输出敏感词扫描 → 审计三事件 → 响应。
"""
import json
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from fastapi.responses import StreamingResponse

from app.core.config import Settings
from app.core.envelope import (
    ApiError,
    Envelope,
    ErrCode,
    error_envelope,
    ok_envelope,
    warn_envelope,
)
from app.core.tracing import elapsed_ms_since, get_trace_id, now_ms, set_trace_id
from app.dependencies import (
    acl_filter_of,
    enforce_rate_limit,
    get_app_settings,
    get_pipeline,
    get_principal,
)
from app.domain.schemas import AskRequest
from app.services.agent.runner import AgentRunner
from app.services.llm import LLMError
from app.services.pipeline import RagPipeline
from app.services.query.intent import is_obvious_chitchat
from app.services.security.auth import Principal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _use_agent(req: AskRequest, settings: Settings) -> bool:
    return settings.agent_enabled and (req.mode == "agent" or req.session_id is not None)


def _agent_identity(req: AskRequest, principal: Principal) -> tuple[str, str, str]:
    # 身份以认证结果为准（凭证优先），请求体 user_id 仅在免认证模式兜底
    user = principal.user_id if principal.user_id != "anon" else (req.user_id or "anon")
    return principal.tenant_id, user, req.session_id or ""


def _validate_question(req: AskRequest) -> None:
    """空问题兜底（AskRequest 有 min_length=1，这里防的是纯空白）。"""
    if not (req.question or "").strip():
        raise ApiError(ErrCode.QUESTION_EMPTY, "问题不能为空", http_status=400)


def _degraded_warning(result: dict) -> tuple[int, str] | None:
    """降级语义 → warn 信封。

    重排不可用是用户该知道的事（44xx 轻提示）；缓存不可用不是——它无感降级，
    只上报指标（stage-2 §错误码）。
    """
    # 通用知识兜底是用户该知道的事：答案不来自知识库（warn 信封，仍返回结果）
    if result.get("answer_source") == "parametric":
        return (int(ErrCode.PARAMETRIC_FALLBACK), "知识库未收录相关内容，以下回答来自模型自身知识")
    if result.get("reranked") is False:
        return (int(ErrCode.RERANK_DEGRADED), "本次未进行精排，结果可能不够精确")
    return None


def _envelope_for(result: dict, trace_id: str, elapsed_ms: int) -> Envelope:
    """按降级语义选择 ok / warn 信封。"""
    warning = _degraded_warning(result)
    if warning:
        return warn_envelope(result, warning[1], trace_id, elapsed_ms, code=warning[0])
    return ok_envelope(result, trace_id, elapsed_ms)


@router.post("/ask", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def ask(
    req: AskRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    pipeline: RagPipeline = Depends(get_pipeline),
    settings: Settings = Depends(get_app_settings),
) -> Envelope:
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _validate_question(req)
    try:
        if _use_agent(req, settings):
            runner: AgentRunner = request.app.state.agent_runner
            tenant, user, session_id = _agent_identity(req, principal)
            final: dict | None = None
            for event, payload in runner.run(
                req.question, tenant, user, session_id or None,
                doc_name_resolver=request.app.state.knowledge.filename_of,
                acl_filter=acl_filter_of(request, principal),
                trace_id=trace_id,
                # TASK-137：闲聊零成本短路。此前该参数在 Agent 路径**从无生产调用点**，
                # 导致"你好"也白跑 plan→tool→reflect 全循环并计费。
                skip_retrieval=is_obvious_chitchat(req.question),
            ):
                if event == "_final":
                    final = payload
            result = {
                **(final or {"answer": "", "evidence": [], "verdict": "refused"}),
                "trace_id": trace_id,
                "elapsed_ms": elapsed_ms_since(started),
                "session_id": session_id or None,
            }
            return _envelope_for(result, trace_id, result["elapsed_ms"])
        # 线性路径同样挂 ACL 与认证主体（此前固定 default_tenant_id 且无 ACL，是越权缺口）
        tenant, user, _ = _agent_identity(req, principal)
        scoped = pipeline.bind_principal(tenant, user, principal)
        scoped._acl_filter = acl_filter_of(request, principal)
        result = scoped.ask(req.question, tenant, trace_id, req.top_k)
        payload = result.model_dump()
        return _envelope_for(payload, trace_id, result.elapsed_ms)
    except LLMError as exc:
        return error_envelope(exc.code, exc.message, trace_id, elapsed_ms_since(started))
    except ApiError:
        raise  # 业务错误码（如 4301 会话不存在）透传给全局异常处理器
    except Exception:
        logger.exception("ask 异常 trace_id=%s", trace_id)
        return error_envelope(ErrCode.INTERNAL_ERROR, "服务暂时不可用，请稍后再试", trace_id, elapsed_ms_since(started))


@router.post("/ask/stream", dependencies=[Depends(enforce_rate_limit)])
def ask_stream(
    req: AskRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    pipeline: RagPipeline = Depends(get_pipeline),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    _validate_question(req)
    use_agent = _use_agent(req, settings)
    runner: AgentRunner | None = request.app.state.agent_runner if use_agent else None
    tenant, user, session_id = _agent_identity(req, principal)
    acl_filter = acl_filter_of(request, principal)
    # 线性路径的主体绑定与 ACL 与 Agent 路径对齐
    scoped = None if runner is not None else pipeline.bind_principal(tenant, user, principal)
    if scoped is not None:
        scoped._acl_filter = acl_filter

    def event_source():
        try:
            if runner is not None:
                for event, payload in runner.run(
                    req.question, tenant, user, session_id or None,
                    doc_name_resolver=request.app.state.knowledge.filename_of,
                    acl_filter=acl_filter,
                    trace_id=trace_id,
                ):
                    if event == "_final":
                        payload = {**payload, "trace_id": trace_id, "session_id": session_id or None}
                        yield _sse("done", payload)
                    else:
                        yield _sse(event, payload)
            else:
                for event, data in scoped.ask_stream(
                    req.question, tenant, trace_id, req.top_k
                ):
                    yield _sse(event, data)
        except ApiError as exc:
            yield _sse("error", {"code": int(exc.code), "message": exc.message, "trace_id": trace_id})
        except Exception:
            logger.exception("ask/stream 异常 trace_id=%s", trace_id)
            yield _sse("error", {"code": ErrCode.INTERNAL_ERROR, "message": "服务暂时不可用", "trace_id": trace_id})

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": trace_id,
        },
    )


# ---------- TASK-124 · 人在环（HITL）----------


class ResumeRequest(BaseModel):
    """人工介入决策。

    - `hint`：补充线索，注入下一次生成
    - `force_answer`：知悉证据不足并授权作答（标 incomplete）
    - `stop`：终止本次运行
    """

    action: Literal["hint", "force_answer", "stop"] = "force_answer"
    value: str = ""


@router.get("/ask/runs/{run_id}/interrupt", response_model=Envelope)
def get_interrupt(
    run_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_app_settings),
) -> Envelope:
    """查询某个 run 是否停在人工介入点（前端刷新后据此恢复"待补充"卡片）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    runner: AgentRunner = request.app.state.agent_runner
    _assert_run_owner(run_id, principal, settings)
    state = runner.interrupt_state(run_id)
    return ok_envelope(state, trace_id, elapsed_ms_since(started))


@router.post("/ask/runs/{run_id}/resume", dependencies=[Depends(enforce_rate_limit)])
def resume_run(
    run_id: str,
    req: ResumeRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """唤醒挂起的 run，继续把事件流跑完（SSE，与 `/ask/stream` 同构）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    runner: AgentRunner = request.app.state.agent_runner
    tenant, user, _ = _agent_identity(AskRequest(question="-"), principal)
    _assert_run_owner(run_id, principal, settings)
    acl_filter = acl_filter_of(request, principal)
    decision = {"action": req.action, "value": req.value}

    def event_source():
        try:
            for event, payload in runner.resume(
                run_id,
                decision,
                doc_name_resolver=request.app.state.knowledge.filename_of,
                acl_filter=acl_filter,
                trace_id=trace_id,
            ):
                if event == "_final":
                    yield _sse("done", {**payload, "trace_id": trace_id})
                else:
                    yield _sse(event, payload)
        except ValueError as exc:
            yield _sse("error", {
                "code": int(ErrCode.INVALID_INPUT), "message": str(exc), "trace_id": trace_id,
            })
        except ApiError as exc:
            yield _sse("error", {
                "code": int(exc.code), "message": exc.message, "trace_id": trace_id,
            })
        except Exception:
            logger.exception("ask/resume 异常 run_id=%s", run_id)
            yield _sse("error", {
                "code": ErrCode.INTERNAL_ERROR, "message": "服务暂时不可用", "trace_id": trace_id,
            })

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": trace_id,
        },
    )


def _assert_run_owner(run_id: str, principal: Principal, settings: Settings) -> None:
    """run_id 归属校验（TASK-142 纪律：跨主体的 run_id 一律 404，不用 403 避免存在性探测）。

    thread_id 约定为 `{tenant}:{user}:{seq}:{turn}`（见 `_agent_identity` 与
    `thread_id_for`），因此前缀即归属凭证。
    """
    expected = f"{principal.tenant_id}:{principal.user_id}:"
    if not run_id.startswith(expected):
        # 复用 SESSION_NOT_FOUND(4301)：run_id 由 session_id 派生，"归属不符"与
        # "会话不存在"是同一类错误，且 4301 语义已被前端识别。
        raise ApiError(ErrCode.SESSION_NOT_FOUND, "运行不存在或不属于当前用户", http_status=404)


@router.post("/ask/runs/{run_id}/cancel", dependencies=[Depends(enforce_rate_limit)])
def cancel_run(
    run_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_app_settings),
) -> Envelope:
    """中止一个进行中的 Agent run（TASK-126）。

    语义是**停止产生新的工具/模型调用**，而不是掐断正在执行的那一个——
    Python 线程无法被强制中断（见 TASK-119 的技术边界）。因此取消后
    最迟在"当前这一步跑完"时停止，`incomplete=True`。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _assert_run_owner(run_id, principal, settings)
    registry = request.app.state.tool_registry
    registry.cancel(run_id)
    return ok_envelope(
        {"run_id": run_id, "cancelled": True}, trace_id, elapsed_ms_since(started)
    )


@router.get("/ask/{trace_id}/cost", response_model=Envelope)
def ask_cost(
    trace_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """单次问答的成本账单（TASK-134）。

    按阶段与模型归集 token 与金额。`priced=False` 表示价格表未覆盖某个模型，
    此时金额**不可信**（显示为未知而非免费），前端应据此提示。
    """
    _tid = get_trace_id()
    set_trace_id(_tid)
    started = now_ms()
    from app.services.agent.cost import get_ledger

    return ok_envelope(get_ledger().for_trace(trace_id), _tid, elapsed_ms_since(started))
