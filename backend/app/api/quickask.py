"""快问端点（F4 · 单文档「上传即问」）。

- POST   /quickask/upload      上传单个文件 → 解析 → 临时索引（不入知识库）
- GET    /quickask/documents   列出某会话下的临时文档（会话内快速解析）
- GET    /quickask/{doc_id}     查询临时文档状态
- POST   /quickask/ask         针对临时文档提问（复用 /ask/stream 的 SSE 协议）
- DELETE /quickask/{doc_id}    删除临时文档（清向量 + BM25 + 文件）

**会话维度**：上传时可带 `session_id`，临时文档即归属该会话；
带会话提问时若文档属于别的会话 → 404（不泄露存在性）。
未绑定会话的文档仍可在任意会话使用（"上传即问"）。

**权限**：普通登录用户即可使用（与知识库管理面的 admin 门禁不同）。
快问文档不进知识库，不影响他人；但每个文档按 `principal.user_id` 归属，
他人访问一律按"不存在"处理。
"""
import json
from typing import Iterator

from fastapi import APIRouter, Depends, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, get_trace_id, now_ms, set_trace_id
from app.dependencies import enforce_rate_limit, get_pipeline, get_principal
from app.services.pipeline import RagPipeline
from app.services.quickask import QuickAskService
from app.core.identity import Principal
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/quickask", tags=["quickask"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


class QuickAskRequest(BaseModel):
    """快问请求体。"""

    question: str = Field(min_length=1, max_length=2000)
    doc_id: str = Field(min_length=1, max_length=64)
    top_k: int | None = Field(default=None, ge=1, le=10)
    #: 会话维度：文档绑定了会话时，提问必须来自同一会话
    session_id: str | None = Field(default=None, max_length=64)


@router.post("/upload", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
async def quickask_upload(
    file: UploadFile,
    request: Request,
    session_id: str | None = Form(default=None, max_length=64),
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """上传单个文件并建立临时索引（同步解析，不进知识库）。

    `session_id` 可选：给了就把临时文档绑定到该会话（会话内快速解析）。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: QuickAskService = request.app.state.quickask
    content = await file.read()
    data = await service.upload(
        file.filename or "unnamed", content, principal, session_id=session_id or None
    )
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


@router.get("/documents", response_model=Envelope)
def quickask_documents(
    request: Request,
    session_id: str = Query(..., min_length=1, max_length=64),
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """列出某会话下的临时文档（只列本人且仍有效的）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: QuickAskService = request.app.state.quickask
    return ok_envelope(
        service.list_session_docs(session_id, principal), trace_id, elapsed_ms_since(started)
    )


@router.post("/ask", dependencies=[Depends(enforce_rate_limit)])
def quickask_ask(
    req: QuickAskRequest,
    request: Request,
    principal: Principal = Depends(get_principal),
    pipeline: RagPipeline = Depends(get_pipeline),
) -> StreamingResponse:
    """针对临时文档提问：SSE 协议与 /ask/stream 完全一致（前端零改造）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    service: QuickAskService = request.app.state.quickask

    # 归属、有效期与会话维度校验：过期/越权/跨会话一律 404 语义的业务错误
    service.get_meta(req.doc_id, principal, session_id=req.session_id)

    tenant, user = principal.tenant_id, principal.user_id
    scoped = pipeline.bind_principal(tenant, user, principal)

    def event_source() -> Iterator[str]:
        try:
            for event, payload in scoped.ask_stream(
                req.question, tenant, trace_id, req.top_k, doc_id=req.doc_id
            ):
                yield _sse(event, payload)
        except ApiError as exc:
            yield _sse("error", {"code": int(exc.code), "message": exc.message, "trace_id": trace_id})
        except Exception:
            logger.exception("quickask/ask 异常 trace_id=%s", trace_id)
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


@router.get("/{doc_id}", response_model=Envelope)
def quickask_status(
    doc_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """快问临时文档的状态（是否已建索引、剩余有效期）。

    过期 → 404（`QUICK_DOC_EXPIRED`）；TTL 由 `quick_ask_ttl_s` 控制。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: QuickAskService = request.app.state.quickask
    return ok_envelope(service.status(doc_id, principal), trace_id, elapsed_ms_since(started))


@router.delete("/{doc_id}", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def quickask_delete(
    doc_id: str,
    request: Request,
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """删除快问临时文档（连带清理临时块与向量）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: QuickAskService = request.app.state.quickask
    service.delete(doc_id, principal)
    return ok_envelope({"doc_id": doc_id, "deleted": True}, trace_id, elapsed_ms_since(started))
