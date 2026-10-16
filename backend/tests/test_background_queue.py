"""TASK-181：后台任务队列。

要证明的四件事：
1. 队列**有界**（背压拒绝，而不是内存无界增长直到 OOM）；
2. 任务失败**不会杀死 worker**（一个坏任务不能拖垮整个后台）；
3. **优雅停机**（跑完当前任务再退出，不像 daemon 被掐断）；
4. 指标**可观测**（排队深度、失败数、拒绝数）。
"""
import threading
import time

from app.core.tasks import BackgroundQueue


class TestBoundedQueue:
    def test_submit_runs_task(self):
        q = BackgroundQueue(name="t", workers=1, queue_size=10)
        done = threading.Event()
        assert q.submit("job", done.set) is True
        assert done.wait(timeout=3), "任务应被 worker 执行"
        q.shutdown()

    def test_rejects_when_full_does_not_block(self):
        """队列满时立即返回 False，**不阻塞**调用方（否则请求线程会挂住）。"""
        q = BackgroundQueue(name="t", workers=1, queue_size=2)
        release = threading.Event()
        # 占住唯一的 worker
        q.submit("blocker", release.wait)
        time.sleep(0.05)
        # 再塞满队列
        for i in range(2):
            q.submit(f"fill{i}", lambda: time.sleep(0.01))

        t0 = time.perf_counter()
        accepted = q.submit("overflow", lambda: None)
        elapsed = time.perf_counter() - t0

        assert elapsed < 0.5, "队列满时必须立即返回，不能阻塞"
        assert accepted is False, "队列满时应背压拒绝"
        assert q.stats()["rejected"] >= 1
        release.set()
        q.shutdown()

    def test_workers_zero_runs_inline(self):
        """workers=0 是同步降级路径（测试友好，行为与内联一致）。"""
        q = BackgroundQueue(name="inline", workers=0)
        calls = []
        assert q.submit("job", lambda: calls.append(1)) is True
        assert calls == [1], "workers=0 时必须同步执行完"
        assert q.stats()["done"] == 1


class TestFailureIsolation:
    def test_failing_job_does_not_kill_worker(self):
        """一个任务抛异常后，后续任务仍必须被执行。"""
        q = BackgroundQueue(name="t", workers=1, queue_size=10)
        done = threading.Event()

        def boom():
            raise RuntimeError("炸了")

        q.submit("boom", boom)
        q.submit("after", done.set)

        assert done.wait(timeout=3), "worker 被异常杀死了（后续任务没执行）"
        st = q.stats()
        assert st["failed"] == 1 and st["done"] >= 1
        q.shutdown()

    def test_inline_failure_isolated(self):
        q = BackgroundQueue(name="inline", workers=0)
        q.submit("boom", lambda: (_ for _ in ()).throw(RuntimeError("x")))
        calls = []
        q.submit("after", lambda: calls.append(1))
        assert calls == [1], "内联模式的异常不应中断调用方"
        assert q.stats()["failed"] == 1

    def test_recent_errors_recorded(self):
        """失败原因必须留痕——否则"后台任务失败了"没有可排查的线索。"""
        q = BackgroundQueue(name="t", workers=0)
        q.submit("my-job", lambda: (_ for _ in ()).throw(RuntimeError("特定错误信息")))
        errors = q.stats()["recent_errors"]
        assert any("my-job" in e and "特定错误信息" in e for e in errors)


class TestGracefulShutdown:
    def test_shutdown_waits_for_current_job(self):
        """停机时正在执行的任务必须跑完（daemon 会把它掐断，留下半截产物）。"""
        q = BackgroundQueue(name="t", workers=1, queue_size=10)
        started = threading.Event()
        finished = threading.Event()

        def slow():
            started.set()
            time.sleep(0.3)
            finished.set()

        q.submit("slow", slow)
        assert started.wait(timeout=3)
        q.shutdown(wait=True, timeout=5)
        assert finished.is_set(), "停机过程中正在执行的任务被丢弃了"

    def test_shutdown_stops_accepting_effectively(self):
        q = BackgroundQueue(name="t", workers=1, queue_size=10)
        q.shutdown(wait=True, timeout=2)
        assert all(not t.is_alive() for t in q._threads), "worker 应已退出"

    def test_shutdown_is_idempotent(self):
        q = BackgroundQueue(name="t", workers=1)
        q.shutdown()
        q.shutdown()  # 二次调用不应抛

    def test_shutdown_on_inline_queue_is_noop(self):
        q = BackgroundQueue(name="t", workers=0)
        q.shutdown()
        assert q.submit("j", lambda: None) is True


class TestObservability:
    def test_stats_shape(self):
        q = BackgroundQueue(name="reports", workers=1, queue_size=5)
        st = q.stats()
        assert {"name", "queued", "capacity", "running", "done", "failed",
                "rejected", "workers"} <= set(st)
        assert st["name"] == "reports" and st["capacity"] == 5
        q.shutdown()

    def test_depth_reflects_backlog(self):
        q = BackgroundQueue(name="t", workers=1, queue_size=50)
        release = threading.Event()
        q.submit("blocker", release.wait)
        time.sleep(0.05)
        for i in range(5):
            q.submit(f"j{i}", lambda: None)
        assert q.depth >= 1, "排队深度应反映积压"
        release.set()
        q.shutdown()

    def test_avg_duration_reported(self):
        q = BackgroundQueue(name="t", workers=0)
        q.submit("j", lambda: time.sleep(0.01))
        assert q.stats()["avg_duration_ms"] > 0

    def test_concurrent_submits_no_loss(self):
        """多线程并发提交不得丢任务（此前每次裸起线程没有这个保证）。"""
        q = BackgroundQueue(name="t", workers=2, queue_size=500)
        counter = {"n": 0}
        lock = threading.Lock()

        def job():
            with lock:
                counter["n"] += 1

        def submitter():
            for _ in range(50):
                while not q.submit("j", job):
                    time.sleep(0.005)

        threads = [threading.Thread(target=submitter) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        deadline = time.time() + 10
        while counter["n"] < 200 and time.time() < deadline:
            time.sleep(0.02)
        assert counter["n"] == 200, f"并发提交丢任务：{counter['n']}/200"
        q.shutdown()


class TestOrchestratorIntegration:
    def test_submit_goes_through_queue(self, client):
        """报告提交必须走队列（此前是裸起 daemon 线程）。"""
        from app.core.tasks import BackgroundQueue

        q = client.app.state.report_queue
        assert isinstance(q, BackgroundQueue)
        assert q.stats()["name"] == "reports"

    def test_rejected_submission_is_marked(self, client, monkeypatch):
        """队列满时报告应被标为 rejected，而不是让用户以为任务在跑。"""
        q = client.app.state.report_queue
        monkeypatch.setattr(q, "submit", lambda name, fn: False)

        r = client.post("/reports", json={"goal": "测试目标与背景说明"}, headers={"X-Tenant-Id": "default"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data.get("rejected") is True or data.get("status") == "rejected", (
            "被背压拒绝时必须显式标记，不能静默"
        )
