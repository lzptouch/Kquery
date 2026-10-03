"""拒答反馈端点（阶段 5 前端规范 §2.3 / §5）。

- POST /feedback/refusal：提交拒答反馈（类型 + 备注 + 追踪号），返回可追踪工单号；
- GET /feedback/{ticket_id}：查询处理状态。
"""
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.services.validation.feedback import FEEDBACK_TYPES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])


class RefusalFeedbackRequest(BaseModel):
    feedback_type: str = Field(pattern=f"^({'|'.join(sorted(FEEDBACK_TYPES))})$")
    refusal_reason: str = ""
    user_note: str = Field(default="", max_length=500)
    trace_id: str = Field(min_length=1)  # 必填：复现拒答的唯一桥梁


@router.post("/refusal", response_model=Envelope)
def submit_refusal_feedback(req: RefusalFeedbackRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = request.app.state.feedback_store
    ticket = store.create(req.feedback_type, req.refusal_reason, req.user_note, req.trace_id)
    logger.info("拒答反馈已受理: %s（关联 trace=%s）", ticket.ticket_id, req.trace_id)
    return ok_envelope(
        {"ticket_id": ticket.ticket_id, "status": ticket.status},
        trace_id,
        elapsed_ms_since(started),
    )


@router.get("/{ticket_id}", response_model=Envelope)
def get_feedback(ticket_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = request.app.state.feedback_store
    ticket = store.get(ticket_id)
    if ticket is None:
        raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, f"工单不存在: {ticket_id}", http_status=404)
    return ok_envelope(ticket.model_dump(), trace_id, elapsed_ms_since(started))
