"""问答端点。

网关顺序：认证 → Principal → 突发限流（令牌桶）→ Agent 执行
（检索前租户过滤 + 检索后 ACL）→ 响应。

并发模型（批次 1）：端点全部为 async，同步管线经 `PipelineExecutor`
（在飞 32 + 排队 200 + 503 背压）执行；SSE 连接不再占用线程池线程。
"""
import json
import logging
import threading
from collections.abc import Iterator
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
from app.services.pipeline_executor import OverloadedError, PipelineExecutor, raise_overloaded
from app.services.query.intent import is_obvious_chitchat
from app.services.security.auth import Principal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ask"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _use_agent(req: AskRequest, settings: Settings) -> bool:
    # 快问（doc_id）强制走线性路径：Agent 路径的检索没有接 doc_id 过滤，
    # 放行会变成"限定单文档的提问却检索了全库"。
    if req.doc_id:
        return False
    return settings.agent_enabled and (req.mode == "agent" or req.session_id is not None)


def _assert_quick_doc(request: Request, req: AskRequest, principal: Principal) -> None:
    """快问的 doc_id 必须属于当前主体（否则等于越权访问他人临时文档）。

    同时校验**会话维度**：绑定到某个会话的临时文档，只能在该会话里提问
    （对齐 doc-qa 的"会话内快速解析"语义）。未绑定会话的文档不受此限。
    """
    if not req.doc_id:
        return
    service = getattr(request.app.state, "quickask", None)
    if service is None:
        raise ApiError(ErrCode.QUICK_DOC_EXPIRED, "快问功能不可用", http_status=404)
    service.get_meta(req.doc_id, principal, session_id=req.session_id)


def _agent_identity(req: AskRequest, principal: Principal) -> tuple[str, str, str]:
    # 身份以认证结果为准（凭证优先），请求体 user_id 仅在免认证模式兜底
    user = principal.user_id if principal.user_id != "anon" else (req.user_id or "anon")
    return principal.tenant_id, user, req.session_id or ""


def _validate_question(req: AskRequest) -> None:
    """空问题兜底（AskRequest 有 min_length=1，这里防的是纯空白）。"""
    if not (req.question or "").strip():
        raise ApiError(ErrCode.QUESTION_EMPTY, "问题不能为空", http_status=400)


def _auto_title(request: Request, principal: Principal, session_id: str, question: str, answer: str) -> None:
    """首轮自动生成会话标题（F5）。

    只在**首轮**且标题仍是默认的「新对话」时改名——用户手动改过的标题不能被覆盖。
    任何失败都静默：会话标题是锦上添花，不能让问答失败。
    """
    if not session_id or not answer:
        return
    store = getattr(request.app.state, "session_store", None)
    llm = getattr(request.app.state, "llm", None)
    if store is None or llm is None:
        return
    try:
        session = store.get(session_id, principal.tenant_id, principal.user_id)
    except Exception:  # noqa: BLE001 - 读不到会话就不改名
        return
    if len(session.get("turns", [])) > 1:
        return  # 不是首轮
    if (session.get("title") or "新对话") != "新对话":
        return  # 用户已手动命名
    from app.services.llm import generate_title

    try:
        title = generate_title(llm, question, answer)
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话自动命名失败（忽略）: %s", exc)
        return
    if not title:
        return
    try:
        store.rename(session_id, principal.tenant_id, principal.user_id, title)
    except Exception as exc:  # noqa: BLE001
        logger.warning("会话标题写入失败（忽略）: %s", exc)


def _degraded_warning(result: dict) -> tuple[int, str] | None:
    """降级语义 → warn 信封。

    重排不可用是用户该知道的事（44xx 轻提示）；缓存不可用不是——它无感降级，
    只上报指标（ §错误码）。
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
async def ask(
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
    executor: PipelineExecutor = request.app.state.pipeline_executor
    try:
        _assert_quick_doc(request, req, principal)
        if _use_agent(req, settings):
            runner: AgentRunner = request.app.state.agent_runner
            tenant, user, session_id = _agent_identity(req, principal)

            def _collect() -> dict | None:
                final: dict | None = None
                for event, payload in runner.run(
                    req.question, tenant, user, session_id or None,
                    doc_name_resolver=request.app.state.knowledge.filename_of,
                    acl_filter=acl_filter_of(request, principal),
                    trace_id=trace_id,
                    # 闲聊零成本短路。此前该参数在 Agent 路径**从无生产调用点**，
                    # 导致"你好"也白跑 plan→tool→reflect 全循环并计费。
                    skip_retrieval=is_obvious_chitchat(req.question),
                ):
                    if event == "_final":
                        final = payload
                return final

            final = await executor.run_async(_collect)
            result = {
                **(final or {"answer": "", "evidence": [], "verdict": "refused"}),
                "trace_id": trace_id,
                "elapsed_ms": elapsed_ms_since(started),
                "session_id": session_id or None,
            }
            _auto_title(request, principal, session_id, req.question, result.get("answer", ""))
            return _envelope_for(result, trace_id, result["elapsed_ms"])
        # 线性路径同样挂 ACL 与认证主体（此前固定 default_tenant_id 且无 ACL，是越权缺口）
        tenant, user, _ = _agent_identity(req, principal)
        scoped = pipeline.bind_principal(tenant, user, principal)
        scoped._acl_filter = acl_filter_of(request, principal)
        result = await executor.run_async(
            scoped.ask, req.question, tenant, trace_id, req.top_k, doc_id=req.doc_id
        )
        payload = result.model_dump()
        return _envelope_for(payload, trace_id, result.elapsed_ms)
    except OverloadedError:
        raise_overloaded()
    except LLMError as exc:
        return error_envelope(exc.code, exc.message, trace_id, elapsed_ms_since(started))
    except ApiError:
        raise  # 业务错误码（如 4301 会话不存在）透传给全局异常处理器
    except Exception:
        logger.exception("ask 异常 trace_id=%s", trace_id)
        return error_envelope(ErrCode.INTERNAL_ERROR, "服务暂时不可用，请稍后再试", trace_id, elapsed_ms_since(started))


def _stream_response(
    executor: PipelineExecutor,
    factory,
    trace_id: str,
    on_value_error: bool = False,
) -> StreamingResponse:
    """把同步事件工厂包装成带背压/心跳/断线取消的 SSE 响应（批次 1）。

    事件名与顺序保持不变（契约零破坏）；新增的只有 `: ping` 心跳注释行，
    SSE 规范下客户端会忽略未知注释行，不影响既有前端解析。
    """
    cancel = threading.Event()

    async def event_source():
        try:
            async for event, payload in executor.stream_events(factory, cancel):
                if event == "__heartbeat__":
                    yield ": ping\n\n"
                else:
                    yield _sse(event, payload)
        except OverloadedError:
            yield _sse("error", {
                "code": int(ErrCode.RATE_LIMITED), "message": "服务繁忙，请稍后重试",
                "trace_id": trace_id,
            })
        except ValueError as exc:
            if on_value_error:
                yield _sse("error", {
                    "code": int(ErrCode.INVALID_INPUT), "message": str(exc), "trace_id": trace_id,
                })
            else:
                raise
        except ApiError as exc:
            yield _sse("error", {"code": int(exc.code), "message": exc.message, "trace_id": trace_id})
        except Exception:
            logger.exception("SSE 异常 trace_id=%s", trace_id)
            yield _sse("error", {"code": int(ErrCode.INTERNAL_ERROR), "message": "服务暂时不可用", "trace_id": trace_id})

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


@router.post("/ask/stream", dependencies=[Depends(enforce_rate_limit)])
async def ask_stream(
    req: AskRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    pipeline: RagPipeline = Depends(get_pipeline),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    _validate_question(req)
    # F4 快问：doc_id 必须属于当前主体/会话（否则等于越权访问他人临时文档）
    _assert_quick_doc(request, req, principal)
    executor: PipelineExecutor = request.app.state.pipeline_executor
    use_agent = _use_agent(req, settings)
    runner: AgentRunner | None = request.app.state.agent_runner if use_agent else None
    tenant, user, session_id = _agent_identity(req, principal)
    acl_filter = acl_filter_of(request, principal)
    # 线性路径的主体绑定与 ACL 与 Agent 路径对齐
    scoped = None if runner is not None else pipeline.bind_principal(tenant, user, principal)
    if scoped is not None:
        scoped._acl_filter = acl_filter

    def factory() -> Iterator[tuple[str, dict]]:
        if runner is not None:
            last_answer = ""
            for event, payload in runner.run(
                req.question, tenant, user, session_id or None,
                doc_name_resolver=request.app.state.knowledge.filename_of,
                acl_filter=acl_filter,
                trace_id=trace_id,
            ):
                if event == "_final":
                    last_answer = payload.get("answer", "")
                    yield "done", {**payload, "trace_id": trace_id, "session_id": session_id or None}
                else:
                    yield event, payload
            # F5：首轮自动命名（流式结束后补写标题）
            _auto_title(request, principal, session_id, req.question, last_answer)
        else:
            # F4 快问：doc_id 限定检索范围（只查这一个临时文档）
            yield from scoped.ask_stream(
                req.question, tenant, trace_id, req.top_k, doc_id=req.doc_id
            )

    return _stream_response(executor, factory, trace_id)


# ----------  · 人在环（HITL）----------


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
async def resume_run(
    run_id: str,
    req: ResumeRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """唤醒挂起的 run，继续把事件流跑完（SSE，与 `/ask/stream` 同构）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    executor: PipelineExecutor = request.app.state.pipeline_executor
    runner: AgentRunner = request.app.state.agent_runner
    tenant, user, _ = _agent_identity(AskRequest(question="-"), principal)
    _assert_run_owner(run_id, principal, settings)
    acl_filter = acl_filter_of(request, principal)
    decision = {"action": req.action, "value": req.value}

    def factory() -> Iterator[tuple[str, dict]]:
        for event, payload in runner.resume(
            run_id,
            decision,
            doc_name_resolver=request.app.state.knowledge.filename_of,
            acl_filter=acl_filter,
            trace_id=trace_id,
        ):
            if event == "_final":
                yield "done", {**payload, "trace_id": trace_id}
            else:
                yield event, payload

    return _stream_response(executor, factory, trace_id, on_value_error=True)


def _assert_run_owner(run_id: str, principal: Principal, settings: Settings) -> None:
    """run_id 归属校验（ 纪律：跨主体的 run_id 一律 404，不用 403 避免存在性探测）。

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
    """中止一个进行中的 Agent run。

    语义是**停止产生新的工具/模型调用**，而不是掐断正在执行的那一个——
    Python 线程无法被强制中断（见  的技术边界）。因此取消后
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
    """单次问答的成本账单。

    按阶段与模型归集 token 与金额。`priced=False` 表示价格表未覆盖某个模型，
    此时金额**不可信**（显示为未知而非免费），前端应据此提示。
    """
    _tid = get_trace_id()
    set_trace_id(_tid)
    started = now_ms()
    from app.services.agent.cost import get_ledger

    # KQ-02：成本账单含模型与金额信息，必须按租户收敛（跨租户读取即越权）
    return ok_envelope(
        get_ledger().for_trace(trace_id, tenant_id=principal.tenant_id),
        _tid, elapsed_ms_since(started),
    )
