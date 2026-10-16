"""后台任务队列（TASK-181）。

## 问题

此前"跑个后台活"就是 `threading.Thread(target=..., daemon=True).start()`，
散落在报告生成（每次提交一个）、Agent 运行（每次请求一个）等处。三个后果：

1. **无上限**：QPS 上来就是线程数线性增长，直到耗尽内存或 GIL 争抢到不可用；
2. **无背压**：请求照单全收，用户看到"已提交"但实际排在几百个任务后面；
3. **不可观测**：排队多深、失败多少、平均耗时多久——全都没有数字。

## 本模块提供

- **有界队列 + 背压**：队列满时`submit` 返回 False（调用方据此给用户明确反馈），
  而不是默默把任务堆进内存；
- **可观测**：`stats()` 返回 queued / running / done / failed / rejected；
- **优雅停机**：`shutdown()` 让 worker 跑完当前任务再退出（daemon 线程做不到——
  进程退出时它们被直接掐断，正在写的文件会留下半截）。

## 与"真队列"的边界

这仍是**进程内**队列，不是 Celery/RQ。多副本下各进程各有一份队列，
任务不会跨副本调度——那需要 Redis/RabbitMQ 与独立的 worker 进程（见 TASK-165/181
的完整形态）。本模块解决的是"线程无上限 + 不可观测 + 不优雅停机"，
把它们收敛到统一的、**可替换**的抽象上，将来换真队列只需换一个实现。
"""
import logging
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_SIZE = 200      # 排队上限（超出即背压，避免内存无界增长）
DEFAULT_WORKERS = 2           # 默认并发 worker 数
RECENT_ERRORS_KEEP = 20       # 保留最近若干条失败原因供排障


@dataclass
class _Job:
    name: str
    fn: Callable[[], None]
    # TASK-132：提交时的上下文快照（含 trace_id）。worker 线程的上下文来自
    # 启动期（lifespan），若不显式携带，后台任务的日志会打上错误的/空的 trace_id，
    # 排障时"用户提交了报告"与"报告生成失败"logs 串不起来。
    ctx: object = None


class BackgroundQueue:
    """有界后台任务队列。

    `workers=0` 时 `submit()` 同步执行（测试与单机轻量部署的降级路径）——
    这样测试不必等待异步完成，也不会引入时序抖动。
    """

    def __init__(
        self,
        name: str = "bg",
        workers: int = DEFAULT_WORKERS,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ):
        self._name = name
        self._workers = max(0, workers)
        self._queue: queue.Queue[_Job | None] = queue.Queue(maxsize=max(1, queue_size))
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._running = 0
        self._done = 0
        self._failed = 0
        self._rejected = 0
        self._recent_errors: deque = deque(maxlen=RECENT_ERRORS_KEEP)
        self._stopping = threading.Event()
        self._durations: deque = deque(maxlen=500)  # 最近 500 次耗时，用于估算
        if self._workers:
            self._start_workers()

    # ---- 生命周期 ----

    def _start_workers(self) -> None:
        for i in range(self._workers):
            t = threading.Thread(
                target=self._loop, name=f"{self._name}-worker-{i}", daemon=True
            )
            t.start()
            self._threads.append(t)

    def _loop(self) -> None:
        while True:
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if job is None:  # 停机哨兵
                self._queue.task_done()
                return
            with self._lock:
                self._running += 1
            started = time.perf_counter()
            try:
                # 在提交方的上下文里执行（trace_id 等 contextvars 随之传播）
                if job.ctx is not None:
                    job.ctx.run(job.fn)
                else:
                    job.fn()
                with self._lock:
                    self._done += 1
            except Exception as exc:  # noqa: BLE001 - 单个任务失败不应杀死 worker
                logger.exception("后台任务失败: %s", job.name)
                with self._lock:
                    self._failed += 1
                    self._recent_errors.append(f"{job.name}: {exc}")
            finally:
                self._durations.append(time.perf_counter() - started)
                with self._lock:
                    self._running -= 1
                self._queue.task_done()

    def shutdown(self, wait: bool = True, timeout: float = 10.0) -> None:
        """优雅停机：让 worker 跑完**当前**任务再退出（不接新任务）。

        与 daemon 线程的区别：daemon 在进程退出时被直接掐断，正在写文件的任务
        会留下半截内容（而我们的入库/报告都要落盘）。
        """
        self._stopping.set()
        if not self._workers:
            return
        # 唤醒所有阻塞在 get() 上的 worker
        for _ in range(self._workers):
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
        if wait:
            deadline = time.time() + timeout
            for t in self._threads:
                remaining = max(0.0, deadline - time.time())
                t.join(timeout=remaining)

    # ---- 提交 ----

    def submit(self, name: str, fn: Callable[[], None]) -> bool:
        """提交任务。返回 False 表示**被背压拒绝**（调用方应告知用户稍后重试）。

        不阻塞、不无限排队：宁可明确拒绝，也不要让内存无界增长直到 OOM——
        那时连"拒绝"的能力都没有了。
        """
        if not self._workers:
            # 无 worker 档：同步执行（测试友好，行为与"内联"完全一致）
            # 耗时同样要记录——否则内联模式下"平均耗时"永远是 0，
            # 而这个指标正是判断"后台任务是否变慢"的依据。
            started = time.perf_counter()
            try:
                fn()
                with self._lock:
                    self._done += 1
            except Exception as exc:  # noqa: BLE001
                logger.exception("后台任务失败（内联）: %s", name)
                with self._lock:
                    self._failed += 1
                    self._recent_errors.append(f"{name}: {exc}")
            finally:
                self._durations.append(time.perf_counter() - started)
            return True
        try:
            import contextvars

            self._queue.put_nowait(
                _Job(name=name, fn=fn, ctx=contextvars.copy_context())
            )
            return True
        except queue.Full:
            with self._lock:
                self._rejected += 1
            logger.warning("后台队列已满，拒绝任务: %s（队列上限 %d）", name, self._queue.maxsize)
            return False

    # ---- 可观测 ----

    def stats(self) -> dict:
        with self._lock:
            done, failed, running = self._done, self._failed, self._running
            rejected = self._rejected
            errors = list(self._recent_errors)
        durations = list(self._durations)
        return {
            "name": self._name,
            "queued": self._queue.qsize(),
            "capacity": self._queue.maxsize,
            "running": running,
            "done": done,
            "failed": failed,
            "rejected": rejected,
            "workers": self._workers,
            "avg_duration_ms": round(sum(durations) / len(durations) * 1000, 1) if durations else 0.0,
            "recent_errors": errors[-5:],
        }

    @property
    def depth(self) -> int:
        return self._queue.qsize()
