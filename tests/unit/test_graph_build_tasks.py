"""`C-09`：图谱构建后台化（受理即返回 + 状态可查 + 有界排队 + TTL）。

同步构建的病根有两条，测试必须分别钉住：

1. **HTTP 超时 / 后台其实跑完了**：全量重建是分钟级操作，而客户端超时通常
   30~60 秒。现在 `POST /graph/build` 只回 `task_id`，进度由
   `GET /graph/build/{task_id}` 查——所以「状态机是对的」比「构建结果是全的」
   更关键：`queued → running → succeeded/failed` 每一档都要能读到。
2. **一次重建卡住所有后台操作**：同步版本一直占着
   `reject_background_when_busy` 的槽位。移出闸门后，队列满必须给**明确
   503 + Retry-After**，而不是发一个永远 `queued` 的 `task_id`。

失败路径同样不能吞：后台线程里抛异常若只留一条日志，界面会显示「成功」——
这是最难排查的一类问题，所以 `error` 必须落到任务快照里。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services.graph.build_tasks import GraphBuildTask, GraphBuildTaskRegistry

_MD = "# 公司\n\nAcme 公司投资了 Beta 创业公司。Beta 的 CEO 是张三。\n"


# ---------------------------------------------------------------- 测试替身


class _RejectingPool:
    """模拟已 shutdown 的线程池（`submit` 抛 `RuntimeError`）。"""

    def submit(self, fn):  # noqa: ARG002 - 只为模拟签名
        raise RuntimeError("cannot schedule new futures after shutdown")


@pytest.fixture()
def pool():
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gb-test")
    yield executor
    executor.shutdown(wait=False, cancel_futures=True)


def _wait_terminal(reg: GraphBuildTaskRegistry, task_id: str, timeout_s: float = 10.0) -> GraphBuildTask:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        task = reg.get(task_id)
        if task is not None and task.status in ("succeeded", "failed"):
            return task
        time.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 未在 {timeout_s}s 内进入终态")


def _wait_running(reg: GraphBuildTaskRegistry, task_id: str, timeout_s: float = 10.0) -> GraphBuildTask:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        task = reg.get(task_id)
        if task is not None and task.status == "running":
            return task
        time.sleep(0.01)
    raise AssertionError(f"任务 {task_id} 未在 {timeout_s}s 内开始运行")


def _stamp(task: GraphBuildTask, status: str, *, finished_ms: int | None = None) -> GraphBuildTask:
    """白盒地把任务摆到某个状态（只为测 TTL / 排队，不经过线程池）。"""
    task.status = status
    task.stage = status
    task.finished_ms = finished_ms
    return task


# ---------------------------------------------------------------- 状态机


class TestSubmitLifecycle:
    def test_success_records_result_and_timestamps(self, pool):
        reg = GraphBuildTaskRegistry(pool)
        task = reg.submit(
            lambda t: {"entities_upserted": 2},
            tenant_id="t1", doc_id="d1", rebuild_communities=True,
        )
        done = _wait_terminal(reg, task.task_id)

        assert done.status == "succeeded"
        assert done.stage == "done"
        assert done.result == {"entities_upserted": 2}
        assert done.error is None
        assert done.started_ms is not None
        assert done.finished_ms is not None and done.finished_ms >= done.started_ms

        snap = done.snapshot()
        assert snap["task_id"] == task.task_id
        assert snap["doc_id"] == "d1"
        assert snap["rebuild_communities"] is True
        assert snap["elapsed_ms"] is not None and snap["elapsed_ms"] >= 0

    def test_runner_can_report_progress_before_finishing(self, pool):
        """`stage` / `chunk_count` 由 runner 写：卡在哪一段是可诊断信息。"""
        entered = threading.Event()
        release = threading.Event()

        def runner(task: GraphBuildTask) -> dict:
            task.stage = "extracting"
            task.chunk_count = 7
            entered.set()
            release.wait(5)
            return {"chunks_processed": 7}

        reg = GraphBuildTaskRegistry(pool)
        task = reg.submit(runner, tenant_id="t1", doc_id=None, rebuild_communities=False)
        assert entered.wait(5)
        mid = reg.get(task.task_id)
        assert mid.status == "running"
        assert mid.stage == "extracting"
        assert mid.chunk_count == 7
        assert mid.finished_ms is None
        assert mid.snapshot()["elapsed_ms"] is not None, "运行中也要能读到已耗时"

        release.set()
        done = _wait_terminal(reg, task.task_id)
        assert done.status == "succeeded" and done.result == {"chunks_processed": 7}

    def test_failure_is_captured_instead_of_swallowed(self, pool):
        reg = GraphBuildTaskRegistry(pool)

        def boom(_task: GraphBuildTask) -> dict:
            raise ValueError("抽取失败: " + "x" * 800)

        task = reg.submit(boom, tenant_id="t1", doc_id=None, rebuild_communities=False)
        done = _wait_terminal(reg, task.task_id)

        assert done.status == "failed"
        assert done.stage == "failed"
        assert done.result is None
        assert done.error["type"] == "ValueError"
        assert done.error["message"].startswith("抽取失败")
        assert len(done.error["message"]) <= 500, "错误消息要截断，不能把整段堆栈塞进任务表"
        assert done.snapshot()["status"] == "failed"

    def test_runner_returning_none_yields_empty_result(self, pool):
        reg = GraphBuildTaskRegistry(pool)
        task = reg.submit(
            lambda t: None, tenant_id="t1", doc_id=None, rebuild_communities=False,
        )
        done = _wait_terminal(reg, task.task_id)
        assert done.status == "succeeded"
        assert done.result == {}, "无返回值的构建也要给出空 dict 而不是 None"

    def test_task_ids_are_unique(self, pool):
        reg = GraphBuildTaskRegistry(pool)
        ids = {
            reg.submit(lambda t: {}, tenant_id="t1", doc_id=None, rebuild_communities=False).task_id
            for _ in range(3)
        }
        assert len(ids) == 3


# ---------------------------------------------------------------- 排队与上限


class TestBackpressure:
    def test_queue_full_raises_runtime_error(self):
        """`max_pending` 是最简单的护栏：满了就拒，不给永远 queued 的 id。"""
        gate = threading.Event()

        def blocking_runner(_task):
            gate.wait(5)
            return {}

        with ThreadPoolExecutor(max_workers=1) as ex:
            reg = GraphBuildTaskRegistry(ex, max_pending=1)
            first = reg.submit(
                blocking_runner, tenant_id="t1", doc_id=None, rebuild_communities=False,
            )
            _wait_running(reg, first.task_id)

            with pytest.raises(RuntimeError, match="排队已满"):
                reg.submit(
                    blocking_runner, tenant_id="t1", doc_id=None, rebuild_communities=False,
                )

            gate.set()
            assert _wait_terminal(reg, first.task_id).status == "succeeded"

    def test_queue_slot_is_released_after_terminal(self, pool):
        """终态任务不应继续占名额（否则跑过几次之后就永久 503）。"""
        reg = GraphBuildTaskRegistry(pool, max_pending=1)
        first = reg.submit(lambda t: {}, tenant_id="t1", doc_id=None, rebuild_communities=False)
        _wait_terminal(reg, first.task_id)

        second = reg.submit(lambda t: {}, tenant_id="t1", doc_id=None, rebuild_communities=False)
        assert _wait_terminal(reg, second.task_id).status == "succeeded"

    def test_pool_rejection_leaves_no_ghost_task(self):
        """池已关闭时不能留下一条永远 `queued` 的幽灵任务。"""
        reg = GraphBuildTaskRegistry(_RejectingPool())
        with pytest.raises(RuntimeError):
            reg.submit(lambda t: {}, tenant_id="t1", doc_id=None, rebuild_communities=False)
        assert reg.stats()["total_tracked"] == 0


# ---------------------------------------------------------------- TTL 与统计


class TestRetention:
    def test_terminal_task_purged_after_retention(self):
        reg = GraphBuildTaskRegistry(_RejectingPool(), retention_s=60)
        expired = _stamp(
            GraphBuildTask("gb-old", "t1", None, False),
            "succeeded",
            finished_ms=int(time.time() * 1000) - 61_000,
        )
        reg._tasks[expired.task_id] = expired

        assert reg.get("gb-old") is None, "超过保留期的终态任务应被清掉"

    def test_task_within_retention_is_kept(self):
        reg = GraphBuildTaskRegistry(_RejectingPool(), retention_s=60)
        fresh = _stamp(
            GraphBuildTask("gb-fresh", "t1", None, False),
            "succeeded",
            finished_ms=int(time.time() * 1000) - 1_000,
        )
        reg._tasks[fresh.task_id] = fresh
        assert reg.get("gb-fresh") is not None

    def test_running_task_never_purged(self):
        """运行中的任务 `finished_ms` 为空，任何 TTL 都不能把它清掉。"""
        reg = GraphBuildTaskRegistry(_RejectingPool(), retention_s=60)
        running = _stamp(GraphBuildTask("gb-live", "t1", None, False), "running")
        running.created_ms = int(time.time() * 1000) - 10 * 86_400_000
        reg._tasks[running.task_id] = running

        assert reg.get("gb-live") is not None

    def test_stats_counts_by_status(self):
        reg = GraphBuildTaskRegistry(_RejectingPool())
        for task_id, status in (("a", "running"), ("b", "queued"), ("c", "succeeded")):
            reg._tasks[task_id] = _stamp(GraphBuildTask(task_id, "t1", None, False), status)

        stats = reg.stats()
        assert stats["total_tracked"] == 3
        assert stats["inflight"] == {"running": 1, "queued": 1, "succeeded": 1}


# ---------------------------------------------------------------- API 契约


class TestBuildApi:
    def test_post_returns_task_id_and_get_reports_completion(self, client):
        client.post(
            "/knowledge/upload",
            files={"file": ("公司.md", _MD.encode("utf-8"), "text/markdown")},
        )
        resp = client.post("/graph/build", json={"doc_id": None, "rebuild_communities": False})
        assert resp.status_code == 200, resp.text

        data = resp.json()["data"]
        assert data["task_id"].startswith("gb-")
        assert data["doc_id"] is None
        assert data["status"] in ("queued", "running", "succeeded")

        deadline = time.time() + 15
        while time.time() < deadline:
            snap = client.get(f"/graph/build/{data['task_id']}").json()["data"]
            if snap["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.05)
        else:
            raise AssertionError("图谱构建未在 15s 内完成")

        assert snap["status"] == "succeeded", snap
        assert snap["result"]["chunks_processed"] >= 1
        # rebuild_communities=False → 不重算社区（省掉一次全量摘要）
        assert "community" not in snap["result"]
        assert snap["finished_ms"] >= snap["created_ms"]

    def test_unknown_task_returns_task_not_found(self, client):
        resp = client.get("/graph/build/gb-unknown")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == 3301

    def test_other_tenant_task_is_invisible(self, client):
        """任务查询必须带租户断言（跨租户可见等于把构建进度与文档 id 泄露出去）。"""
        reg = client.app.state.graph_build_tasks
        other = _stamp(
            GraphBuildTask("gb-other-tenant", "other", None, False),
            "succeeded",
            finished_ms=int(time.time() * 1000),
        )
        reg._tasks[other.task_id] = other
        try:
            resp = client.get("/graph/build/gb-other-tenant")
            assert resp.status_code == 404
            assert resp.json()["error"]["code"] == 3301
        finally:
            reg._tasks.pop(other.task_id, None)

    def test_queue_full_returns_503_with_retry_after(self, client):
        """排队满 → 503 + Retry-After（背压语义，与后台闸门一致）。"""
        reg = client.app.state.graph_build_tasks
        original_max = reg._max_pending
        reg._max_pending = 1
        busy = _stamp(GraphBuildTask("gb-busy", "default", None, False), "running")
        reg._tasks[busy.task_id] = busy
        try:
            resp = client.post("/graph/build", json={})
            assert resp.status_code == 503, resp.text
            assert resp.json()["error"]["code"] == 6101  # RATE_LIMITED
            assert resp.headers.get("Retry-After"), "503 必须带 Retry-After"
        finally:
            reg._max_pending = original_max
            reg._tasks.pop(busy.task_id, None)

    def test_invalid_doc_is_rejected_before_queueing(self, client):
        """无效目标要同步拒（404），不能先入队再在后台线程里失败。"""
        reg = client.app.state.graph_build_tasks
        before = reg.stats()["total_tracked"]

        resp = client.post("/graph/build", json={"doc_id": "not-exist"})
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == 3002  # DOCUMENT_NOT_FOUND
        assert reg.stats()["total_tracked"] == before, "越权/无效目标不得占用队列名额"
