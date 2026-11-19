"""管线执行器（并发提容批次 1）：进程级有界执行 + 背压 + SSE 桥接。

解决三个问题（并发模型现状见 `doc/analysis/11-并发与容量.md`）：

1. **AnyIO 默认线程池 40 全端点共享**：同步 `def` 端点把 SSE 连接的整个生命周期
   变成线程占用。这里改为进程级固定执行器（默认 32），问答路径独占；
   管理面/探针仍走 AnyIO 池，互不抢占。
2. **无界排队**：在飞 32 + 排队 200，超出立即 503 + Retry-After（快速失败），
   不再把请求隐藏在线程池队列里排队到死。
3. **同步管线与 async 端点桥接**：管线（LLM/Embedding/Milvus）是同步 SDK，
   不做全链路异步重写；worker 线程跑同步事件迭代器，经 asyncio.Queue 投递给
   async 生成器——连接本身不再占用线程池线程（空闲连接只占内存与 fd）。

线程安全说明：worker 线程直接调用 `asyncio.Queue.put_nowait`。这在 CPython 下
依赖 GIL 保证 deque 操作原子性（单生产者、单消费者场景可用）；若将来换
PyPy 或多生产者，需改为 `loop.call_soon_threadsafe`。
"""
import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

_SENTINEL = object()


class OverloadedError(Exception):
    """在飞与排队均已满。调用方映射为 503 + Retry-After。"""


class PipelineExecutor:
    """进程级有界执行器：在飞信号量（线程级）+ 排队上限 + 统计。"""

    def __init__(
        self,
        workers: int = 32,
        queue_size: int = 200,
        on_reject=None,
        low_queue_size: int = 50,
    ) -> None:
        self._workers = max(1, int(workers))
        self._queue_size = max(1, int(queue_size))
        # 低优先级（后台：入库 / 图谱 / 重建）的排队上限更小（`C-11`）：
        # 后台任务可以等，但**不该占满交互流量的排队位**——
        # 否则一次批量导入就能让 /ask 全部 503。
        self._low_queue_size = max(1, int(low_queue_size))
        self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="pipeline")
        self._cond = threading.Condition()
        self._inflight = 0
        self._waiting = 0
        self._rejected = 0
        self._completed = 0
        # 拒绝回调（批次 2 指标）：装配期注入 prom.shed_total 上报
        self._on_reject = on_reject

    def _note_reject(self, reason: str = "overloaded") -> None:
        if self._on_reject is not None:
            try:
                self._on_reject(reason)
            except TypeError:
                # 兼容旧签名（无参回调）
                self._on_reject()
            except Exception:  # noqa: BLE001 - 指标失败不得影响背压
                pass

    # ---- 在飞槽位（线程级原语：worker 线程与 async 层都要用）----

    def acquire(self, timeout_s: float = 30.0, priority: str = "high") -> bool:
        """申请一个在飞槽位。排队已满立即返回 False（不阻塞）。

        `priority`：`high`（交互：/ask）或 `low`（后台：入库 / 图谱 / 重建）。
        低优先级的排队上限更小（`low_queue_size`）——后台任务可以等，
        但不该占满交互流量的排队位。
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        limit = self._queue_size if priority != "low" else self._low_queue_size
        with self._cond:
            if self._inflight < self._workers:
                self._inflight += 1
                return True
            if self._waiting >= limit:
                self._rejected += 1
                self._note_reject("low_priority" if priority == "low" else "overloaded")
                return False
            self._waiting += 1
            try:
                while self._inflight >= self._workers:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._cond.wait(remaining)
                self._inflight += 1
                return True
            finally:
                self._waiting -= 1

    def release(self) -> None:
        with self._cond:
            if self._inflight > 0:
                self._inflight -= 1
                self._completed += 1
            self._cond.notify()

    async def acquire_async(self, timeout_s: float = 30.0, priority: str = "high") -> bool:
        """异步申请在飞槽位：等待不占用 AnyIO 线程（轮询 + 短 sleep）。

        等待本身有 deadline，且排队人数有上限——两个条件任一不满足立即 False。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        limit = self._queue_size if priority != "low" else self._low_queue_size
        with self._cond:
            if self._inflight < self._workers:
                self._inflight += 1
                return True
            if self._waiting >= limit:
                self._rejected += 1
                self._note_reject("low_priority" if priority == "low" else "overloaded")
                return False
            self._waiting += 1
        try:
            while loop.time() < deadline:
                with self._cond:
                    if self._inflight < self._workers:
                        self._inflight += 1
                        return True
                await asyncio.sleep(0.02)
            return False
        finally:
            with self._cond:
                self._waiting -= 1

    # ---- 同步函数执行 ----

    def run(self, fn: Callable, *args, timeout_s: float = 30.0, priority: str = "high", **kwargs):
        """申请槽位后在池中执行同步函数（调用线程阻塞等待结果）。"""
        if not self.acquire(timeout_s=timeout_s, priority=priority):
            raise OverloadedError
        try:
            future = self._pool.submit(fn, *args, **kwargs)
            return future.result()
        finally:
            self.release()

    async def run_async(self, fn: Callable, *args, timeout_s: float = 30.0, priority: str = "high", **kwargs):
        """异步入口：申请槽位不阻塞事件循环。"""
        acquired = await self.acquire_async(timeout_s, priority=priority)
        if not acquired:
            raise OverloadedError
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._pool, lambda: fn(*args, **kwargs))
        finally:
            self.release()

    # ---- SSE 桥接 ----

    async def stream_events(
        self,
        event_iter_factory: Callable[[], Iterator[tuple[str, dict]]],
        cancel: threading.Event,
        heartbeat_s: float = 15.0,
        queue_size: int = 512,
        timeout_s: float = 30.0,
    ):
        """把同步事件迭代器桥接成 async 生成器。

        worker 线程跑同步迭代器并把 (event, payload) 投入 asyncio.Queue；
        async 侧按序取出。超过 heartbeat_s 无事件则产出一个心跳项。
        消费者退出（客户端断开）时在 finally 里置 cancel，worker 在下一个
        事件前停止（已发出的上游调用无法中断，这是明确的技术边界）。
        """
        loop = asyncio.get_running_loop()
        acquired = await self.acquire_async(timeout_s)
        if not acquired:
            raise OverloadedError
        queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)

        def produce() -> None:
            try:
                for item in event_iter_factory():
                    if cancel.is_set():
                        break
                    try:
                        queue.put_nowait(item)
                    except asyncio.QueueFull:
                        # 消费者慢于生产者（慢网络）：停止生成而不是把线程堵死
                        cancel.set()
                        break
            except Exception as exc:  # noqa: BLE001 - 错误也要送抵消费者
                try:
                    queue.put_nowait(exc)
                except asyncio.QueueFull:
                    pass
            finally:
                try:
                    queue.put_nowait(_SENTINEL)
                except asyncio.QueueFull:
                    pass
                self.release()

        loop.run_in_executor(self._pool, produce)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=heartbeat_s)
                except TimeoutError:
                    yield "__heartbeat__", {}
                    continue
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            cancel.set()

    def stats(self) -> dict:
        with self._cond:
            return {
                "workers": self._workers,
                "queue_size": self._queue_size,
                # 低优先级排队上限（`C-11`）与当前深度
                "queue_size_low": self._low_queue_size,
                "inflight": self._inflight,
                "waiting": self._waiting,
                "rejected_total": self._rejected,
                "completed_total": self._completed,
            }

    def shutdown(self, timeout_s: float = 30.0) -> None:
        """lifespan 收尾：等在飞任务结束（有上限），不再接受新任务。"""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while self._inflight > 0 and time.monotonic() < deadline:
                self._cond.wait(0.5)
        self._pool.shutdown(wait=False, cancel_futures=True)


def raise_overloaded() -> None:
    """503 + Retry-After 的统一出口（保持错误码与信封一致）。"""
    # 按需载入：仅在特定路径用到
    from app.core.envelope import ApiError, ErrCode

    raise ApiError(
        ErrCode.RATE_LIMITED,
        "服务繁忙，请稍后重试",
        http_status=503,
        retry_after=5,
    )
