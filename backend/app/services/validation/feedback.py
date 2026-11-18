"""拒答反馈工单。

- 反馈与追踪号绑定：没有追踪号，运营无法复现这次拒答；
- 状态机：待处理 → 处理中 → 已处理（本阶段提供创建与查询，流转由运营侧接管）；
- 高频被反馈的问题 = 知识库迭代最直接的输入。
- **归属**：工单是用户数据，按 `tenant_id` / `user_id` 落库并按主体查询；
  认证下线后主体恒为 `default/local`，但字段必须存在——否则多租户回归时
  `GET /feedback/{ticket_id}` 就是一个只按 id 取的越权面（见 `tests/security/`）。
"""
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel
from app.core.identity import DEFAULT_TENANT_ID, DEFAULT_USER_ID
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

TICKET_PENDING = "pending"
TICKET_PROCESSING = "processing"
TICKET_RESOLVED = "resolved"

# 用户视角的拒答反馈类型
FEEDBACK_TYPES = {"should_answer", "wrong_reason", "missing_doc", "other"}


class FeedbackTicket(BaseModel):
    ticket_id: str
    feedback_type: str  # should_answer（应该能答）/ wrong_reason / missing_doc / other
    refusal_reason: str = ""
    user_note: str = ""
    trace_id: str  # 关联的请求追踪号（必填：复现拒答的唯一桥梁）
    status: str = TICKET_PENDING
    #: 归属维度。历史工单文件没有这两个字段，默认值让它们仍可被固定身份读到。
    tenant_id: str = DEFAULT_TENANT_ID
    user_id: str = DEFAULT_USER_ID
    created_at: str = ""
    handled_at: str = ""
    resolution: str = ""


class FeedbackStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._tickets: dict[str, FeedbackTicket] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._tickets = {t["ticket_id"]: FeedbackTicket.model_validate(t) for t in raw}
        except Exception:  # noqa: BLE001 - 反馈工单加载失败：按空工单启动（历史工单已丢失，需人工确认）
            logger.warning("反馈工单加载失败：按空工单启动（历史工单已丢失，需人工确认）")
            self._tickets = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([t.model_dump() for t in self._tickets.values()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def create(
        self,
        feedback_type: str,
        refusal_reason: str,
        user_note: str,
        trace_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        user_id: str = DEFAULT_USER_ID,
    ) -> FeedbackTicket:
        ticket = FeedbackTicket(
            ticket_id=f"fb-{uuid.uuid4().hex[:10]}",
            feedback_type=feedback_type,
            refusal_reason=refusal_reason,
            user_note=user_note,
            trace_id=trace_id,
            tenant_id=tenant_id,
            user_id=user_id,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._tickets[ticket.ticket_id] = ticket
            self._save()
        return ticket

    def get(
        self,
        ticket_id: str,
        tenant_id: str = DEFAULT_TENANT_ID,
        user_id: str = DEFAULT_USER_ID,
    ) -> FeedbackTicket | None:
        """按 id + 主体取工单；归属不符与不存在同返回 None（不泄漏存在性）。"""
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return None
        if ticket.tenant_id != tenant_id or ticket.user_id != user_id:
            return None
        return ticket
