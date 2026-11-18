"""图谱构建任务的受理与状态（`C-09`）。

## 为什么必须后台化

`POST /graph/build` 原来是**同步**的：请求线程里跑「取块 → LLM 抽取实体关系 →
实体消解（含向量比较）→ 社区检测」。全量重建在真实库上是一分钟量级，而
HTTP 客户端（含反代）默认超时通常是 30~60 秒——于是最典型的现象是
**「前端转圈 → 超时 → 后台其实跑完了」**，用户重试一次就再跑一遍。

更糟的是它占着 `reject_background_when_busy` 的名额：一次全量重建期间，
入库和其它后台操作全被拒。

## 设计取舍

- **单 worker 串行**：图谱写入本身是要拿全局锁的（`C-08` 的锁计时就是证据），
  并发构建只会让锁等待更长、还可能出现「两个构建互相覆盖实体消解表」。
  用 1 个 worker 是最简单的互斥，且天然 FIFO。
- **排队上限**：超过 `max_pending` 直接 503，而不是无限排队——排队没有成本，
  但用户拿到的 task_id 会永远 `queued`，比明确拒绝更糟。
- **状态只在内存 + TTL**：这是**可重放**的操作（重建图谱不改变源数据），
  丢了任务态最多是「不知道进度」，重跑即可。落盘反而要处理跨进程并发。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Literal

from app.core.logging_setup import get_logger

logger = get_logger(__name__)

TaskStatus = Literal["queued", "running", "succeeded", "failed"]


@dataclass
class GraphBuildTask:
    """一次图谱构建的可观测状态。"""

    task_id: str
    tenant_id: str
    doc_id: str | None
    rebuild_communities: bool
    status: TaskStatus = "queued"
    created_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    started_ms: int | None = None
    finished_ms: int | None = None
    # 进度：构建过程分三段，卡在哪一段是可诊断信息
    stage: str = "queued"
    chunk_count: int | None = None
    result: dict | None = None
    error: dict | None = None

    def snapshot(self) -> dict:
        elapsed = None
        if self.started_ms is not None:
            end = self.finished_ms if self.finished_ms is not None else int(time.time() * 1000)
            elapsed = end - self.started_ms
        return {
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "doc_id": self.doc_id,
            "rebuild_communities": self.rebuild_communities,
            "chunk_count": self.chunk_count,
            "created_ms": self.created_ms,
            "started_ms": self.started_ms,
            "finished_ms": self.finished_ms,
            "elapsed_ms": elapsed,
            "result": self.result,
            "error": self.error,
        }


class GraphBuildTaskRegistry:
    """受理、排队、执行、查询图谱构建任务（进程内，带 TTL）。"""

    def __init__(
        self,
        pool,
        retention_s: int = 1800,
        max_pending: int = 4,
    ) -> None:
        self._pool = pool
        self._retention_ms = max(60, int(retention_s)) * 1000
        self._max_pending = max(1, int(max_pending))
        self._lock = threading.Lock()
        self._tasks: dict[str, GraphBuildTask] = {}

    # ---- 查询 ----
    def get(self, task_id: str) -> GraphBuildTask | None:
        with self._lock:
            self._purge_locked()
            return self._tasks.get(task_id)

    def stats(self) -> dict:
        with self._lock:
            self._purge_locked()
            counts: dict[str, int] = {}
            for task in self._tasks.values():
                counts[task.status] = counts.get(task.status, 0) + 1
            return {"inflight": counts, "total_tracked": len(self._tasks)}

    def _purge_locked(self) -> None:
        """清掉超过保留期的**终态**任务（运行中的永远不删）。"""
        now = int(time.time() * 1000)
        dead = [
            tid for tid, task in self._tasks.items()
            if task.status in ("succeeded", "failed")
            and task.finished_ms is not None
            and now - task.finished_ms > self._retention_ms
        ]
        for tid in dead:
            self._tasks.pop(tid, None)

    # ---- 受理 ----
    def submit(
        self,
        runner: Callable[[GraphBuildTask], dict],
        *,
        tenant_id: str,
        doc_id: str | None,
        rebuild_communities: bool,
    ) -> GraphBuildTask:
        """建任务并投池；池拒绝时抛 `RuntimeError`（调用方转 503）。

        `runner` 在 worker 线程里执行，返回 `result` dict；抛异常的 task 记为
        `failed` 并保留错误信息（不吞异常——「重建失败但界面显示成功」是
        最难排查的一类问题）。
        """
        task = GraphBuildTask(
            task_id=f"gb-{uuid.uuid4().hex[:12]}",
            tenant_id=tenant_id,
            doc_id=doc_id,
            rebuild_communities=rebuild_communities,
        )
        with self._lock:
            self._purge_locked()
            pending = sum(1 for t in self._tasks.values() if t.status in ("queued", "running"))
            if pending >= self._max_pending:
                raise RuntimeError(f"图谱构建排队已满（{pending}/{self._max_pending}）")
            self._tasks[task.task_id] = task

        def _wrapped() -> None:
            task.status = "running"
            task.stage = "loading_chunks"
            task.started_ms = int(time.time() * 1000)
            try:
                task.result = runner(task) or {}
                task.status = "succeeded"
                task.stage = "done"
            except Exception as exc:  # noqa: BLE001 - 后台任务必须吞住，否则线程静默死
                task.status = "failed"
                task.stage = "failed"
                task.error = {"type": type(exc).__name__, "message": str(exc)[:500]}
                logger.exception("图谱构建任务失败: %s", task.task_id)
            finally:
                task.finished_ms = int(time.time() * 1000)

        try:
            self._pool.submit(_wrapped)
        except RuntimeError:
            with self._lock:
                self._tasks.pop(task.task_id, None)
            raise
        return task
