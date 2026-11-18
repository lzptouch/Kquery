"""入库任务淘汰（`A-06`）。

## 问题

任务表 `_tasks` **只增不减**：任务进终态（完成 / 失败 / 部分失败 / 取消）后
一直留在内存里。连续上传 600 个文档 → 内存里挂着 600 个任务对象，
每个还带着文件级记录。

## 语义

- 终态任务保留一段时间（默认 30 分钟）供前端查看进度；
- 超过保留期 → 从内存淘汰（文件原件不动，落盘的任务记录仍在）；
- **排队中 / 执行中的任务永不淘汰**（淘汰掉等于让用户的任务凭空消失）。
"""
from __future__ import annotations

import time

from app.domain.schemas import TASK_CANCELLED, TASK_DONE, TASK_FAILED, TASK_QUEUED, IngestTask
from app.services.ingest import TaskManager


class _FakeKnowledge:
    """只需要构造 TaskManager 用到的接口。"""

    def get(self, doc_id):  # pragma: no cover - 淘汰测试不触发
        raise KeyError(doc_id)


def _manager(tmp_path, retention_s: int = 1800) -> TaskManager:
    return TaskManager(
        _FakeKnowledge(),
        tmp_path / "tasks.json",
        tmp_path / "ingest",
        workers=0,
        retention_s=retention_s,
    )


def _task(task_id: str, status: str) -> IngestTask:
    return IngestTask(task_id=task_id, status=status, files=[], total_files=0)


def test_terminal_tasks_are_evicted_after_retention(tmp_path):
    """终态任务超过保留期 → 淘汰。"""
    m = _manager(tmp_path, retention_s=60)
    m._tasks["done"] = _task("done", TASK_DONE)
    m._terminal_at["done"] = time.time() - 120  # 2 分钟前完成

    evicted = m.evict_terminal_tasks()
    assert evicted == 1
    assert "done" not in m._tasks
    assert "done" not in m._cancel_flags
    assert "done" not in m._terminal_at


def test_terminal_tasks_within_retention_are_kept(tmp_path):
    """保留期内 → 不淘汰（前端还要看进度）。"""
    m = _manager(tmp_path, retention_s=1800)
    m._tasks["done"] = _task("done", TASK_DONE)
    m._terminal_at["done"] = time.time() - 60

    assert m.evict_terminal_tasks() == 0
    assert "done" in m._tasks


def test_running_and_queued_tasks_are_never_evicted(tmp_path):
    """排队中 / 执行中的任务永不淘汰。"""
    m = _manager(tmp_path, retention_s=1)
    m._tasks["queued"] = _task("queued", TASK_QUEUED)
    m._terminal_at["queued"] = time.time() - 10_000
    m._tasks["failed"] = _task("failed", TASK_FAILED)
    m._terminal_at["failed"] = time.time() - 10_000
    m._tasks["cancelled"] = _task("cancelled", TASK_CANCELLED)
    m._terminal_at["cancelled"] = time.time() - 10_000

    evicted = m.evict_terminal_tasks()
    assert evicted == 2  # failed + cancelled
    assert "queued" in m._tasks, "排队中的任务不能被淘汰"


def test_restored_tasks_get_a_timestamp_instead_of_being_dropped(tmp_path):
    """进程重启后从磁盘恢复的终态任务没有时间戳 → 按「现在」算，不立即清掉。"""
    m = _manager(tmp_path, retention_s=60)
    m._tasks["restored"] = _task("restored", TASK_DONE)  # 无 _terminal_at

    assert m.evict_terminal_tasks() == 0
    assert "restored" in m._tasks
    assert "restored" in m._terminal_at


def test_eviction_disabled_when_retention_zero(tmp_path):
    """保留期设为 0 → 不淘汰（显式关闭）。"""
    m = _manager(tmp_path, retention_s=0)
    m._tasks["done"] = _task("done", TASK_DONE)
    m._terminal_at["done"] = time.time() - 10_000
    assert m.evict_terminal_tasks() == 0
    assert "done" in m._tasks


def test_default_retention_is_thirty_minutes(tmp_path):
    """默认保留 30 分钟（够前端看完进度，也够运维复盘）。"""
    m = _manager(tmp_path)
    assert m._retention_s == 1800
