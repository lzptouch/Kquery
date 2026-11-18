"""查询分析记录存储。

- 全量留存意图统计（不含原文即可），原话记录按最小化原则随对话策略滚动；
- 提供列表/详情/模式统计三个查询面，支撑前端历史页与模式分析卡片；
- 记录带策略版本：换策略后回放旧记录需注明口径。
"""
import json
import threading
import time
from pathlib import Path

from app.services.query.schemas import QueryAnalysis
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

MAX_RECORDS = 1000  # 滚动保留上限（最小化原则）


class AnalysisStore:
    def __init__(self, path: Path, flush_interval_s: float = 2.0) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._records: list[QueryAnalysis] = []
        self._dirty = False
        self._flush_interval_s = max(0.5, flush_interval_s)
        self._stopping = threading.Event()
        self._load()
        self._flusher = threading.Thread(
            target=self._flush_loop, name="analysis-flush", daemon=True
        )
        self._flusher.start()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._records = [QueryAnalysis.model_validate(r) for r in raw]
        except Exception as exc:
            logger.warning("分析记录文件损坏（忽略并重置）: %s", exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([r.model_dump() for r in self._records], ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def save(self, analysis: QueryAnalysis) -> None:
        """请求路径只改内存（批次 1）：此前每次问答都把最多 1000 条记录整体
        JSON dump 一次（O(记录数) 写放大），500 QPS 下会成为全局串行点。
        落盘由后台线程按脏标记批量 flush（崩溃最多丢最近 flush 间隔内的记录，
        分析记录是可再生的观测数据，不是合规审计）。"""
        with self._lock:
            self._records.append(analysis)
            if len(self._records) > MAX_RECORDS:
                self._records = self._records[-MAX_RECORDS:]
            self._dirty = True

    def _flush_loop(self) -> None:
        while not self._stopping.wait(self._flush_interval_s):
            self.flush()

    def flush(self) -> None:
        """立即落盘（后台线程与 lifespan 收尾调用；测试也可直接调）。"""
        with self._lock:
            if not self._dirty:
                return
            try:
                self._save()
                self._dirty = False
            except OSError as exc:
                logger.warning("分析记录落盘失败（下轮重试）: %s", exc)

    def close(self) -> None:
        self._stopping.set()
        self.flush()

    def get(self, analysis_id: str, tenant_id: str | None = None) -> QueryAnalysis | None:
        """按 id 取记录；`tenant_id` 给定时归属不符与不存在同返回 None。

        分析记录含**用户原话**，跨租户按 id 直取等于读别人的提问历史
        （对照 `doc/analysis` KQ-02：按 id 取资源必须先断言租户）。
        """
        for r in self._records:
            if r.analysis_id != analysis_id:
                continue
            if tenant_id and r.tenant_id != tenant_id:
                return None
            return r
        return None

    def list_records(
        self,
        intent: str | None = None,
        needs_retrieval: bool | None = None,
        range_s: int | None = None,
        limit: int = 50,
        tenant_id: str | None = None,
    ) -> list[QueryAnalysis]:
        cutoff = time.time() - range_s if range_s else None
        rows = []
        for r in reversed(self._records):  # 最新在前
            if tenant_id and r.tenant_id != tenant_id:
                continue
            if intent and r.intent != intent:
                continue
            if needs_retrieval is not None and r.needs_retrieval != needs_retrieval:
                continue
            if cutoff:
                try:
                    ts = time.mktime(time.strptime(r.created_at[:19], "%Y-%m-%dT%H:%M:%S"))
                except Exception:  # noqa: BLE001 - 时间戳解析失败：按当前时间处理
                    logger.debug("时间戳解析失败：按当前时间处理")
                    ts = time.time()
                if ts < cutoff:
                    continue
            rows.append(r)
            if len(rows) >= limit:
                break
        return rows

    def patterns(self, range_s: int | None = None, tenant_id: str | None = None) -> list[dict]:
        """意图分布统计（Query 模式分析卡片）：intent / count / ratio / avg_elapsed_ms。"""
        records = self.list_records(range_s=range_s, limit=MAX_RECORDS, tenant_id=tenant_id)
        if not records:
            return []
        buckets: dict[str, list[int]] = {}
        for r in records:
            buckets.setdefault(r.intent, []).append(r.elapsed_ms)
        total = len(records)
        return sorted(
            (
                {
                    "intent": intent,
                    "count": len(times),
                    "ratio": round(len(times) / total, 4),
                    "avg_elapsed_ms": sum(times) // len(times),
                }
                for intent, times in buckets.items()
            ),
            key=lambda x: -x["count"],
        )
