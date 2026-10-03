"""会话管理端点（阶段 6 前端规范 §五）。

会话标识一律视为不可信输入：服务端校验"租户+用户"归属后才放行。
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.core.envelope import Envelope, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.dependencies import get_session_store
from app.services.agent.sessions import SessionStore

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _identity(request: Request) -> tuple[str, str]:
    tenant = request.headers.get("X-Tenant-Id", "default")
    user = request.headers.get("X-User-Id", "anon")
    return tenant, user


class CreateSessionRequest(BaseModel):
    title: str = Field(default="新对话", max_length=50)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=50)


@router.get("", response_model=Envelope)
def list_sessions(
    request: Request, store: SessionStore = Depends(get_session_store)
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant, user = _identity(request)
    return ok_envelope(store.list_sessions(tenant, user), trace_id, elapsed_ms_since(started))


@router.post("", response_model=Envelope)
def create_session(
    req: CreateSessionRequest,
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant, user = _identity(request)
    session = store.create(tenant, user, req.title)
    return ok_envelope({"session_id": session["session_id"], "title": session["title"]}, trace_id, elapsed_ms_since(started))


@router.get("/{session_id}", response_model=Envelope)
def get_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> Envelope:
    """加载会话历史（切换会话时恢复消息与证据）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant, user = _identity(request)
    session = store.get(session_id, tenant, user)
    return ok_envelope(
        {
            "session_id": session["session_id"],
            "title": session["title"],
            "turns": session.get("turns", []),
            "updated_at": session["updated_at"],
        },
        trace_id,
        elapsed_ms_since(started),
    )


@router.patch("/{session_id}", response_model=Envelope)
def rename_session(
    session_id: str,
    req: RenameSessionRequest,
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant, user = _identity(request)
    session = store.rename(session_id, tenant, user, req.title)
    return ok_envelope({"session_id": session["session_id"], "title": session["title"]}, trace_id, elapsed_ms_since(started))


@router.delete("/{session_id}", response_model=Envelope)
def delete_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> Envelope:
    """删除会话：快照与工具调用明细一并删除（不留残留原文）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant, user = _identity(request)
    store.delete(session_id, tenant, user)
    return ok_envelope({"deleted": True}, trace_id, elapsed_ms_since(started))
