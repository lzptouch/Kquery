"""会话管理端点。

会话标识一律视为不可信输入：服务端校验"租户+用户"归属后才放行。
"""
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.envelope import Envelope, ok_envelope
from app.core.tracing import elapsed_ms_since, get_trace_id, now_ms, set_trace_id
from app.dependencies import enforce_rate_limit, get_principal, get_session_store
from app.services.agent.checkpointer import (
    delete_session_threads,
    resolve_checkpoint_db_path,
)
from app.services.agent.sessions import SessionStore

router = APIRouter(prefix="/sessions", tags=["sessions"], dependencies=[Depends(get_principal)])


def _identity(request: Request) -> tuple[str, str]:
    """会话归属以认证主体为准，不可信请求头伪造（开启真实认证后 principal 才可信）。"""
    principal = request.state.principal
    return principal.tenant_id, principal.user_id


class CreateSessionRequest(BaseModel):
    title: str = Field(default="新对话", max_length=50)


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=50)


@router.get("", response_model=Envelope)
def list_sessions(
    request: Request, store: SessionStore = Depends(get_session_store)
) -> Envelope:
    """会话列表（按更新时间倒序，分页）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant, user = _identity(request)
    return ok_envelope(store.list_sessions(tenant, user), trace_id, elapsed_ms_since(started))


@router.post("", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def create_session(
    req: CreateSessionRequest,
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> Envelope:
    """新建会话（拿到 `session_id` 后即可带 `session_id` 提问以启用多轮上下文）。"""
    trace_id = get_trace_id()
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
    trace_id = get_trace_id()
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


@router.patch("/{session_id}", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def rename_session(
    session_id: str,
    req: RenameSessionRequest,
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> Envelope:
    """重命名会话标题（自动命名失败时也可用它手动改）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant, user = _identity(request)
    session = store.rename(session_id, tenant, user, req.title)
    return ok_envelope({"session_id": session["session_id"], "title": session["title"]}, trace_id, elapsed_ms_since(started))


@router.delete("/{session_id}", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def delete_session(
    session_id: str,
    request: Request,
    store: SessionStore = Depends(get_session_store),
) -> Envelope:
    """删除会话：快照、工具调用明细、**LangGraph 检查点**一并删除。

    检查点这一层此前漏了：删掉快照后，每轮的完整 state（对话原文 + 证据 +
    工具出入参）仍留在 `agent_checkpoints.db` 里，等 7 天 TTL 才消失。用户以为
    "会话删了"，实际磁盘上还在——删除承诺的是立刻不留残留（`KQ-23` 残余）。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant, user = _identity(request)
    store.delete(session_id, tenant, user)
    # 检查点删除失败不阻断会话删除：快照已经没了，报错只会让用户重试时更困惑
    checkpoints_deleted = 0
    db_path = resolve_checkpoint_db_path(get_settings())
    if db_path is not None:
        checkpoints_deleted = delete_session_threads(db_path, session_id)
    return ok_envelope(
        {"deleted": True, "checkpoints_deleted": checkpoints_deleted},
        trace_id,
        elapsed_ms_since(started),
    )
