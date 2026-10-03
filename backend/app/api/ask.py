"""问答端点（阶段 1 §4 + 阶段 6 Agentic + 阶段 9 安全网关）。

网关顺序（§10.1）：认证 → Principal → 配额（滑动窗口）→ Agent 执行
（检索前租户过滤 + 检索后 ACL）→ 输出敏感词扫描 → 审计三事件 → 响应。
"""
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.core.config import Settings
from app.core.envelope import ApiError, Envelope, ErrCode, error_envelope, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.dependencies import (
    enforce_rate_limit,
    get_agent_runner,
    get_app_settings,
    get_pipeline,
    get_principal,
)
from app.domain.schemas import AskRequest
from app.services.agent.runner import AgentRunner
from app.services.llm import LLMError
from app.services.pipeline import RagPipeline
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


def _acl_filter_of(request: Request, principal: Principal):
    """构造检索后 ACL 过滤函数（缺文档一律拒绝——默认拒绝原则）。"""
    settings: Settings = request.app.state.settings
    if not settings.acl_enabled:
        return None
    acl = request.app.state.acl_service
    knowledge = request.app.state.knowledge

    def check(doc_id: str) -> bool:
        if doc_id.startswith("__"):  # 系统内置块（社区摘要/图像/计算器）租户内可见
            return True
        try:
            doc = knowledge.get(doc_id)
        except Exception:  # noqa: BLE001
            return False  # 无主文档不可见
        return acl.check(principal, doc)

    return check


@router.post("/ask", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def ask(
    req: AskRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    pipeline: RagPipeline = Depends(get_pipeline),
    settings: Settings = Depends(get_app_settings),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    try:
        if _use_agent(req, settings):
            runner: AgentRunner = request.app.state.agent_runner
            tenant, user, session_id = _agent_identity(req, principal)
            final: dict | None = None
            for event, payload in runner.run(
                req.question, tenant, user, session_id or None,
                doc_name_resolver=request.app.state.knowledge.filename_of,
                acl_filter=_acl_filter_of(request, principal),
                trace_id=trace_id,
            ):
                if event == "_final":
                    final = payload
            result = {
                **final,
                "trace_id": trace_id,
                "elapsed_ms": elapsed_ms_since(started),
                "from_cache": False,
                "reranked": True,
                "retrieval": None,
                "session_id": session_id or None,
            }
            return ok_envelope(result, trace_id, result["elapsed_ms"])
        result = pipeline.ask(req.question, settings.default_tenant_id, trace_id, req.top_k)
        return ok_envelope(result.model_dump(), trace_id, result.elapsed_ms)
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
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    use_agent = _use_agent(req, settings)
    runner: AgentRunner | None = request.app.state.agent_runner if use_agent else None
    tenant, user, session_id = _agent_identity(req, principal)
    acl_filter = _acl_filter_of(request, principal)

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
                for event, data in pipeline.ask_stream(
                    req.question, settings.default_tenant_id, trace_id, req.top_k
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
