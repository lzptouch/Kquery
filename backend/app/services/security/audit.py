"""审计日志（阶段 9 §4 / §8.4）。

三条硬约束：
1. 不存原文——问题与答案只存 16 位 hex 哈希（可追溯但不可读）；
2. 不存命中词——敏感词只记录类别与数量（词表本身即敏感信息）；
3. 只追加——JSONL 追加写，无修改删除接口（审计库独立，业务侧只写）。

三事件共享 trace_id：request / retrieve / answer。一次请求可能多轮检索，
检索事件可出现多条——按 trace_id 聚合展示时序。
"""
import hashlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def content_hash(text: str) -> str:
    """16 位 hex（64 位）：足以追溯到具体内容，不可逆。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AuditStore:
    def __init__(self, audit_path: Path):
        self._path = audit_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(
        self,
        event: str,
        trace_id: str,
        tenant_id: str,
        user_id: str,
        payload: dict,
    ) -> dict:
        """只追加。payload 由调用方保证已脱敏（hash/计数，不含原文）。"""
        record = {
            "event_id": uuid.uuid4().hex[:12],
            "trace_id": trace_id,
            "event": event,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "payload": payload,
            "created_at": time.time(),
        }
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return record

    # ---- 三类业务事件 ----

    def log_request(self, trace_id: str, tenant_id: str, user_id: str, question: str) -> None:
        self.append("rag.request", trace_id, tenant_id, user_id, {
            "question_hash": content_hash(question),
            "question_len": len(question),
        })

    def log_retrieve(self, trace_id: str, tenant_id: str, user_id: str, retrieved: int, visible: int) -> None:
        self.append("rag.retrieve", trace_id, tenant_id, user_id, {
            "retrieved": retrieved, "visible": visible,
        })

    def log_answer(
        self, trace_id: str, tenant_id: str, user_id: str, answer: str, sensitive_hits: int
    ) -> None:
        self.append("rag.answer", trace_id, tenant_id, user_id, {
            "answer_hash": content_hash(answer),
            "answer_len": len(answer),
            "sensitive_hits": sensitive_hits,
        })

    def log_admin(self, trace_id: str, tenant_id: str, user_id: str, action: str, detail: dict) -> None:
        """管理操作也写审计（审计查询者本身被审计，防监督退化为风险点）。"""
        self.append("admin.action", trace_id, tenant_id, user_id, {"action": action, **detail})

    # ---- 查询 ----

    def query(
        self,
        since: float | None = None,
        until: float | None = None,
        event: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        trace_id: str | None = None,
        limit: int = 200,
    ) -> list[dict]:
        if not self._path.exists():
            return []
        records: list[dict] = []
        with self._lock:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):  # 最新在前
            try:
                record = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if since and record["created_at"] < since:
                continue
            if until and record["created_at"] > until:
                continue
            if event and record["event"] != event:
                continue
            if tenant_id and record["tenant_id"] != tenant_id:
                continue
            if user_id and record["user_id"] != user_id:
                continue
            if trace_id and record["trace_id"] != trace_id:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    def export_csv(self, **filters) -> str:
        """导出 CSV（不含原文，仅 hash + 元数据）。"""
        records = self.query(limit=10000, **filters)
        header = "created_at,event,trace_id,tenant_id,user_id,payload"
        rows = [
            f'{r["created_at"]},{r["event"]},{r["trace_id"]},{r["tenant_id"]},{r["user_id"]},"{json.dumps(r["payload"], ensure_ascii=False).replace(chr(34), chr(39))}"'
            for r in records
        ]
        return header + "\n" + "\n".join(rows)
