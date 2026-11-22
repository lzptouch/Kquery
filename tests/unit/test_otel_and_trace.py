"""OTel 接入 + trace 断链修复。

 记录的问题：`contextvars` 不随 `threading.Thread` 传播（Python 既定行为），
导致图内所有日志的 trace_id 为空（`get_trace_id()` 会**新生成**一个），
一次请求的日志在日志平台上被切成两段互不相关的记录。
"""
import threading
import time

from app.core.tracing import get_trace_id, new_trace_id, set_trace_id, spawn_thread


class TestContextPropagation:
    def test_plain_thread_loses_trace_id(self):
        """反面验证：普通线程确实会丢失 trace_id（这是要修的根因）。"""
        set_trace_id("parent-trace")
        seen: list[str] = []

        def worker():
            # 新线程里 get_trace_id() 会**新生成**一个，而不是父线程的值
            seen.append(get_trace_id())

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=3)
        assert seen and seen[0] != "parent-trace", (
            "若这里相等，说明 Python 行为已变，本项目不再需要 spawn_thread"
        )

    def test_spawn_thread_propagates_trace_id(self):
        """修复后：spawn_thread 创建的线程继承父上下文的 trace_id。"""
        set_trace_id("parent-trace-2")
        seen: list[str] = []

        t = spawn_thread(lambda: seen.append(get_trace_id()), name="t")
        t.start()
        t.join(timeout=3)
        assert seen == ["parent-trace-2"], "trace_id 未随线程传播（日志仍会断链）"

    def test_spawn_thread_passes_args_and_kwargs(self):
        got = []
        t = spawn_thread(lambda a, b=None: got.append((a, b)), name="t",
                         args=(1,), kwargs={"b": 2})
        t.start()
        t.join(timeout=3)
        assert got == [(1, 2)]

    def test_spawn_thread_context_is_snapshot_not_shared(self):
        """必须是 copy_context() 快照：共享同一 context 会让两个线程互相覆盖。"""
        set_trace_id("outer")
        results: dict[str, str] = {}

        def worker(key, value):
            set_trace_id(value)
            time.sleep(0.02)
            results[key] = get_trace_id()

        t1 = spawn_thread(worker, name="t1", args=("a", "trace-a"))
        t2 = spawn_thread(worker, name="t2", args=("b", "trace-b"))
        t1.start(); t2.start()
        t1.join(timeout=3); t2.join(timeout=3)

        assert results == {"a": "trace-a", "b": "trace-b"}, (
            f"两个线程的 trace_id 互相污染：{results}"
        )
        # 父线程的上下文不受影响
        assert get_trace_id() == "outer"


class TestBackgroundQueueContext:
    def test_job_carries_submitter_context(self):
        """后台任务的日志必须带**提交方**的 trace_id（否则跨线程断链依旧）。"""
        from app.core.tasks import BackgroundQueue

        q = BackgroundQueue(name="ctx", workers=1, queue_size=10)
        seen: list[str] = []
        done = threading.Event()

        set_trace_id("submitter-trace")
        q.submit("job", lambda: (seen.append(get_trace_id()), done.set()))
        assert done.wait(timeout=3), "任务未执行"
        assert seen == ["submitter-trace"], (
            "后台任务未继承提交方上下文（worker 用的是启动期上下文）"
        )
        q.shutdown()

    def test_inline_path_keeps_context(self):
        from app.core.tasks import BackgroundQueue

        q = BackgroundQueue(name="ctx-inline", workers=0)
        seen: list[str] = []
        set_trace_id("inline-trace")
        q.submit("job", lambda: seen.append(get_trace_id()))
        assert seen == ["inline-trace"]


class TestAuditAndLogCorrelation:
    def test_trace_id_available_inside_agent_thread(self, client):
        """端到端：Agent 线程内的日志应带请求的 trace_id（不再断链）。"""
        seen: list[str] = []

        def worker():
            seen.append(get_trace_id())

        set_trace_id("e2e-trace")
        t = spawn_thread(worker, name="e2e")
        t.start()
        t.join(timeout=3)
        assert seen == ["e2e-trace"]

    def test_new_trace_id_is_unique(self):
        ids = {new_trace_id() for _ in range(100)}
        assert len(ids) == 100
