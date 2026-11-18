"""上游隔离仓与熔断（并发提容批次 2）。

目标：慢上游不拖死快路径，已知坏的上游不再消耗线程与超时预算。

- **隔离仓（bulkhead）**：每类上游一个信号量，许可数按"上游配额"配置
  （之和必须 ≤ 问答执行器线程数，否则线程全被等许可占满）。
- **熔断器**：window 次窗口内失败率超过阈值 → 打开 open_seconds；
  打开期间**快速失败**（不占线程、不等超时）；半开后 half_open 次探测全成功才关闭。
- **自适应并发（可选，默认关）**：AIMD——连续 50 次成功 +1，超时 -30%，
  在 [1, permits] 间伸缩。配置 `upstream_adaptive` 开启。
- **NullBulkhead**：测试或未配置时为零开销直通，不改变既有行为。
"""
import threading
import time
from collections import deque
from app.core.logging_setup import get_logger

logger = get_logger(__name__)


class CircuitOpenError(Exception):
    """熔断打开：调用方应映射为降级错误，而不是占用线程等超时。"""

    def __init__(self, upstream: str, retry_after_s: float) -> None:
        super().__init__(f"上游 {upstream} 熔断打开，{retry_after_s:.0f}s 后重试")
        self.upstream = upstream
        self.retry_after_s = retry_after_s


class Bulkhead:
    def __init__(
        self,
        name: str,
        permits: int,
        window: int = 20,
        fail_ratio: float = 0.5,
        open_seconds: float = 30.0,
        half_open: int = 3,
        adaptive: bool = False,
    ) -> None:
        self.name = name
        self._max_permits = max(1, int(permits))
        self._permits = float(self._max_permits)
        self._window = max(2, int(window))
        self._fail_ratio = min(1.0, max(0.01, fail_ratio))
        self._open_seconds = max(1.0, open_seconds)
        self._half_open = max(1, half_open)
        self._adaptive = adaptive

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._in_use = 0
        self._outcomes: deque[bool] = deque(maxlen=self._window)
        self._state = "closed"  # closed | open | half_open
        self._opened_at = 0.0
        self._half_open_ok = 0
        self._success_streak = 0
        # 调用耗时滚动窗口（`C-10`）：降级判据需要「上游最近有多慢」，
        # 而不是「熔断开没开」——熔断是事后，P95 是事前。
        self._latencies: deque[float] = deque(maxlen=200)
        # 统计
        self._rejected = 0
        self._circuit_opens = 0
        self._calls = 0
        self._failures = 0

    # ---- 熔断状态 ----

    def _circuit_allows(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.monotonic() - self._opened_at >= self._open_seconds:
                self._state = "half_open"
                self._half_open_ok = 0
                return True
            return False
        # half_open：只允许 half_open 个并发探测
        return self._in_use < self._half_open

    def _record(self, ok: bool) -> None:
        self._calls += 1
        if not ok:
            self._failures += 1
        # AIMD（仅开启时生效）
        if self._adaptive:
            if ok:
                self._success_streak += 1
                if self._success_streak >= 50:
                    self._permits = min(float(self._max_permits), self._permits + 1)
                    self._success_streak = 0
            else:
                self._success_streak = 0
                self._permits = max(1.0, self._permits * 0.7)
        if self._state == "half_open":
            if ok:
                self._half_open_ok += 1
                if self._half_open_ok >= self._half_open:
                    self._state = "closed"
                    self._outcomes.clear()
            else:
                self._state = "open"
                self._opened_at = time.monotonic()
            return
        self._outcomes.append(ok)
        if len(self._outcomes) == self._window:
            fails = sum(1 for o in self._outcomes if not o)
            if fails / len(self._outcomes) >= self._fail_ratio:
                self._state = "open"
                self._opened_at = time.monotonic()
                self._circuit_opens += 1
                logger.warning(
                    "上游 %s 熔断打开（窗口 %d 次失败 %d 次，%ds 后半开）",
                    self.name, self._window, fails, int(self._open_seconds),
                )

    # ---- 许可 ----

    def acquire(self, timeout_s: float | None = None):
        """获取一个上游调用许可。熔断打开立即抛 CircuitOpenError。"""
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        with self._cond:
            while True:
                if not self._circuit_allows():
                    self._rejected += 1
                    remaining = self._open_seconds - (time.monotonic() - self._opened_at)
                    raise CircuitOpenError(self.name, max(1.0, remaining))
                if self._in_use < int(self._permits):
                    self._in_use += 1
                    return _Permit(self, started_at=time.monotonic())
                if deadline is not None and time.monotonic() >= deadline:
                    self._rejected += 1
                    raise CircuitOpenError(self.name, 0.0)
                self._cond.wait(0.1)

    def _release(self, ok: bool, elapsed_ms: float | None = None) -> None:
        with self._cond:
            if self._in_use > 0:
                self._in_use -= 1
            if elapsed_ms is not None:
                self._latencies.append(elapsed_ms)
            self._record(ok)
            self._cond.notify()

    def stats(self) -> dict:
        with self._lock:
            return {
                "permits": int(self._permits),
                "max_permits": self._max_permits,
                "in_use": self._in_use,
                # 占用率（0~1）：外部只拿到 in_use 时算不出「是否打满」
                "utilization": (
                    round(self._in_use / self._max_permits, 4) if self._max_permits else 0.0
                ),
                "state": self._state,
                "calls": self._calls,
                "failures": self._failures,
                "rejected": self._rejected,
                "circuit_opens": self._circuit_opens,
                # 上游耗时 P95（毫秒）与样本数（`C-10` 降级判据的数据源）
                "p95_ms": self._p95_ms(),
                "latency_samples": len(self._latencies),
            }

    def _p95_ms(self) -> float:
        """滚动窗口的 P95 耗时（毫秒）。样本不足时返回 0（不据此降级）。"""
        samples = sorted(self._latencies)
        if not samples:
            return 0.0
        idx = min(len(samples) - 1, int(len(samples) * 0.95))
        return round(samples[idx], 2)


class _Permit:
    def __init__(self, bulkhead: Bulkhead, started_at: float | None = None) -> None:
        self._bulkhead = bulkhead
        self._started_at = started_at

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = (
            (time.monotonic() - self._started_at) * 1000.0 if self._started_at is not None else None
        )
        self._bulkhead._release(ok=exc_type is None, elapsed_ms=elapsed_ms)
        return False


class NullBulkhead:
    """未配置时的直通实现（零行为变化）。"""

    name = "null"

    def acquire(self, timeout_s: float | None = None):
        return _NullPermit()

    def stats(self) -> dict:
        return {
            "permits": 0, "max_permits": 0, "in_use": 0, "utilization": 0.0,
            "state": "null", "p95_ms": 0.0, "latency_samples": 0,
        }


class _NullPermit:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


# ---- 进程级注册表 ----

_registry: dict[str, Bulkhead] = {}
_registry_lock = threading.Lock()


def configure_bulkheads(settings) -> None:
    """按配置初始化隔离仓（lifespan 启动时调用一次）。"""
    global _registry
    adaptive = bool(getattr(settings, "upstream_adaptive", False))
    window = int(getattr(settings, "upstream_breaker_window", 20))
    ratio = float(getattr(settings, "upstream_breaker_fail_ratio", 0.5))
    open_s = float(getattr(settings, "upstream_breaker_open_s", 30.0))
    with _registry_lock:
        _registry = {
            name: Bulkhead(
                name,
                permits=int(getattr(settings, f"upstream_{name}_permits", 0) or 0),
                window=window, fail_ratio=ratio, open_seconds=open_s,
                adaptive=adaptive,
            )
            # 通道清单：`multimodal` 是图像向量 / VLM 直答那一路
            # （`C-07`：此前只注册 4 条，多模态打满在指标上完全不可见）
            for name in ("llm", "embedding", "rerank", "milvus", "multimodal")
            if int(getattr(settings, f"upstream_{name}_permits", 0) or 0) > 0
        }
        if _registry:
            logger.info(
                "上游隔离仓已启用：%s（adaptive=%s）",
                ", ".join(f"{k}={v.stats()['max_permits']}" for k, v in _registry.items()),
                adaptive,
            )


def bulkhead(name: str):
    """取隔离仓；未配置该上游时返回 NullBulkhead（直通，零行为变化）。"""
    with _registry_lock:
        return _registry.get(name) or NullBulkhead()


def bulkheads_stats() -> dict:
    with _registry_lock:
        return {name: bh.stats() for name, bh in _registry.items()}
