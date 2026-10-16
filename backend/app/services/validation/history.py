"""校验链路的历史落库（stage-5 §8.2）。

CRAG 轮次与自检明细此前只随响应返回，事后无法复盘"这次为什么被拒答"、
"剔除了哪些声明"。这里按 JSONL 追加落盘，按 trace_id 可回查。

刻意不做成关系表：这两类数据的写入是问答链路的副作用而非业务主数据，
按 append-only 的行存更省事，也避免把主链路拖进数据库事务。

保留策略：按文件滚动（默认 20MB 一份），读侧只看最近若干份。
"""
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CRAG_KIND = "crag_round"
SELFCHECK_KIND = "self_check"


class ValidationHistoryStore:
    """CRAG 轮次 + 自检明细的追加式存储。"""

    def __init__(
        self,
        directory: Path,
        keep_files: int = 20,
        max_bytes: int = 20 * 1024 * 1024,
    ):
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._keep = max(1, keep_files)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()

    # ---- 写 ----

    def _current_file(self) -> Path:
        files = sorted(self._dir.glob("validation-*.jsonl"))
        if files and files[-1].stat().st_size < self._max_bytes:
            return files[-1]
        return self._dir / f"validation-{int(time.time())}.jsonl"

    def append(self, kind: str, payload: dict[str, Any]) -> None:
        """追加一条记录。落库失败不得影响问答（只记日志）。"""
        record = {"kind": kind, "ts": time.time(), **payload}
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            logger.warning("校验历史序列化失败（忽略）: %s", exc)
            return
        try:
            with self._lock:
                with self._current_file().open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                self._rotate()
        except OSError as exc:
            logger.warning("校验历史落库失败（不影响问答）: %s", exc)

    def _rotate(self) -> None:
        files = sorted(self._dir.glob("validation-*.jsonl"))
        for stale in files[: max(0, len(files) - self._keep)]:
            try:
                stale.unlink()
            except OSError:
                logger.warning("校验历史滚动删除失败: %s", stale)

    # ---- 读 ----

    def read(self, kind: str | None = None, limit: int = 500) -> list[dict]:
        out: list[dict] = []
        for path in sorted(self._dir.glob("validation-*.jsonl"), reverse=True):
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning("跳过损坏的校验历史行: %s", path.name)
                        continue
                    if kind is None or record.get("kind") == kind:
                        out.append(record)
            except OSError as exc:
                logger.warning("校验历史读取失败: %s", exc)
            if len(out) >= limit:
                break
        out.sort(key=lambda r: r.get("ts", 0), reverse=True)
        return out[:limit]

    def by_trace(self, trace_id: str) -> dict[str, list[dict]]:
        """按 trace_id 聚合：{'crag_round': [...], 'self_check': [...]}（自检只留最后一次）。"""
        buckets: dict[str, list[dict]] = {CRAG_KIND: [], SELFCHECK_KIND: []}
        for record in self.read(limit=100_000):
            if record.get("trace_id") != trace_id:
                continue
            kind = record.get("kind")
            if kind == SELFCHECK_KIND:
                buckets[SELFCHECK_KIND] = [record]
            elif kind in buckets:
                buckets[kind].append(record)
        buckets[CRAG_KIND].sort(key=lambda r: r.get("round", 0))
        return buckets
