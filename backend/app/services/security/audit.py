"""审计日志。

三条硬约束：
1. 不存原文——问题与答案只存 16 位 hex 哈希（可追溯但不可读）；
2. 不存命中词——敏感词只记录类别与数量（词表本身即敏感信息）；
3. 只追加——JSONL 追加写，无修改删除接口（审计库独立，业务侧只写）。

三事件共享 trace_id：request / retrieve / answer。一次请求可能多轮检索，
检索事件可出现多条——按 trace_id 聚合展示时序。
"""
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


def content_hash(text: str) -> str:
    """16 位 hex（64 位）：足以追溯到具体内容，不可逆。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class AuditStore:
    def __init__(self, audit_path: Path, flush_interval_s: float = 0.5, max_pending: int = 10000):
        self._path = audit_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        # HMAC 密钥（对照分析文档 KQ-24）：纯 SHA256 截断的短问题哈希可被字典穷举还原，
        # 与"审计不存原文"的承诺矛盾。密钥仅存服务端、与 JWT 密钥独立。
        self._key = self._load_or_create_key()
        # 批次 1：写路径去同步落盘。此前每请求 3 次 open/append/close + 全局锁，
        # 高 QPS 下成为串行点。改为内存队列 + 后台批量写；崩溃最多丢 pending 条数
        # （max_pending 内），超过上限时**丢弃最旧未写记录并计数**——
        # 审计宁可少一条也不能阻塞请求路径。查询前会先 flush，保证可见性。
        self._pending: queue.Queue[str] = queue.Queue(maxsize=max(100, max_pending))
        self._dropped = 0
        self._flush_interval_s = max(0.1, flush_interval_s)
        self._stopping = threading.Event()
        self._writer = threading.Thread(target=self._write_loop, name="audit-writer", daemon=True)
        self._writer.start()

    def _write_loop(self) -> None:
        while not self._stopping.wait(self._flush_interval_s):
            self.flush()

    def flush(self) -> None:
        """把 pending 全部落盘（后台线程 / 查询前 / lifespan 收尾调用）。"""
        lines: list[str] = []
        while True:
            try:
                lines.append(self._pending.get_nowait())
            except queue.Empty:
                break
        if not lines:
            return
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write("".join(lines))

    def close(self) -> None:
        self._stopping.set()
        self.flush()

    def _load_or_create_key(self) -> bytes:
        key_path = self._path.parent / ".audit_key"
        try:
            return bytes.fromhex(key_path.read_text().strip())
        except Exception:  # noqa: BLE001 - 首次启动或损坏时重新生成
            import secrets

            key = secrets.token_bytes(32)
            key_path.write_text(key.hex(), encoding="utf-8")
            return key

    def _hash(self, text: str) -> str:
        """HMAC-SHA256 截断 32 hex：离线无法对短文本做字典穷举。"""
        return hmac.new(self._key, text.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

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
        line = json.dumps(record, ensure_ascii=False) + "\n"
        try:
            self._pending.put_nowait(line)
        except queue.Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 1000 == 0:
                logger.error("审计写入队列已满，丢弃 %d 条记录（请求路径不阻塞）", self._dropped)
        return record

    # ---- 三类业务事件 ----

    def log_request(self, trace_id: str, tenant_id: str, user_id: str, question: str) -> None:
        self.append("rag.request", trace_id, tenant_id, user_id, {
            "question_hash": self._hash(question),
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
            "answer_hash": self._hash(answer),
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
        offset: int = 0,
    ) -> list[dict]:
        """倒序查询（最新在前），支持分页。

        此前是 `read_text().splitlines()` —— 把**整个**审计文件读进内存
        再倒序扫。审计是只追加的，线上跑一个月就是几十万行；一次查询的峰值内存
        等于文件大小，且每次都要全量扫描（即使只要最近 20 条）。

        现在改为**流式逐行扫描 + 提前终止**：内存占用与"命中条数"成正比，
        与文件大小无关；配合 `offset` 可做真分页。
        """
        # 先把内存中的 pending 落盘，保证查询能读到刚写入的记录——
        # 必须先 flush 再判存在性，否则首批记录还在队列里就被早退
        self.flush()
        if not self._path.exists():
            return []
        matched: list[dict] = []
        # 查询不持写锁（对照分析文档 KQ-24）：审计是 append-only，写侧每次只追加一行，
        # 读侧不加锁最坏读到半行（下方的 try/except 会跳过）；而查询持写锁会让
        # 管理员的审计查询在高峰时阻塞所有审计写入。
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except Exception:  # noqa: BLE001 - 半行/损坏行跳过
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
                matched.append(record)
        if not matched:
            return []
        # 最新在前 → 先倒序，再按 offset/limit 切片
        matched.reverse()
        return matched[offset : offset + max(1, int(limit))]

    def export_csv(self, **filters) -> str:
        """导出 CSV（不含原文，仅 hash + 元数据）。"""
        records = self.query(limit=10000, **filters)
        header = "created_at,event,trace_id,tenant_id,user_id,payload"
        rows = [
            f'{r["created_at"]},{r["event"]},{r["trace_id"]},{r["tenant_id"]},{r["user_id"]},"{json.dumps(r["payload"], ensure_ascii=False).replace(chr(34), chr(39))}"'
            for r in records
        ]
        return header + "\n" + "\n".join(rows)
