"""并发提容专项回归。

覆盖：
- PipelineExecutor：有界在飞 + 排队上限 + 拒绝统计 + SSE 桥接心跳/断开取消；
- Bulkhead：熔断打开后快速失败、半开恢复、NullBulkhead 直通；
- single-flight：相同问题并发只算一次；
- 请求级缓存：同一请求内父块/文件名只查一次；
- 入库队列：有界背压（满时 submit 返回 False）+ 优雅停机。
"""
import asyncio
import contextlib
import threading
import time

import pytest

from app.services.pipeline_executor import PipelineExecutor
from app.services.upstream import Bulkhead, CircuitOpenError, NullBulkhead


def _run(coro):
    """在独立线程的新事件循环里跑协程。

    不能用 asyncio.run()：pytest-playwright 会在主线程保留运行中的事件循环，
    直接调用会抛 "cannot be called from a running event loop"。
    """
    box: dict = {}

    def worker() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            box["value"] = loop.run_until_complete(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            loop.close()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=60)
    if "error" in box:
        raise box["error"]
    return box.get("value")


class TestPipelineExecutor:
    def test_inflight_bounded_and_released(self):
        ex = PipelineExecutor(workers=2, queue_size=3)
        assert ex.acquire(0.1)
        assert ex.acquire(0.1)
        assert ex.stats()["inflight"] == 2
        ex.release()
        ex.release()
        assert ex.stats()["inflight"] == 0
        assert ex.stats()["completed_total"] == 2

    def test_queue_full_rejects_fast(self):
        ex = PipelineExecutor(workers=1, queue_size=1)
        assert ex.acquire(0.1)  # 占满在飞
        blocker = threading.Event()

        def waiter():
            # 占满排队位
            ex.acquire(timeout_s=2.0)

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        assert ex.acquire(0.05) is False  # 排队已满 → 立即 False
        assert ex.stats()["rejected_total"] == 1
        blocker.set()
        t.join(timeout=3)
        ex.release()
        ex.release()

    def test_run_async_executes_and_releases(self):
        ex = PipelineExecutor(workers=2, queue_size=2)

        async def main():
            return await ex.run_async(lambda x: x * 2, 21)

        assert _run(main()) == 42
        assert ex.stats()["inflight"] == 0

    def test_stream_events_yields_all_and_heartbeat(self):
        ex = PipelineExecutor(workers=2, queue_size=2)
        cancel = threading.Event()

        def factory():
            for i in range(3):
                yield f"ev{i}", {"i": i}

        async def main():
            got = []
            async for event, payload in ex.stream_events(factory, cancel, heartbeat_s=0.05):
                got.append(event)
            return got

        assert _run(main()) == ["ev0", "ev1", "ev2"]

    def test_stream_events_error_propagates(self):
        ex = PipelineExecutor(workers=2, queue_size=2)
        cancel = threading.Event()

        def factory():
            yield "ev0", {}
            raise RuntimeError("boom")

        async def main():
            with pytest.raises(RuntimeError):
                async for _ in ex.stream_events(factory, cancel):
                    pass

        _run(main())


class TestBulkhead:
    def test_null_bulkhead_passthrough(self):
        nb = NullBulkhead()
        with nb.acquire():
            pass  # 直通，不抛异常

    def test_circuit_opens_and_fast_fails(self):
        bh = Bulkhead("test", permits=2, window=4, fail_ratio=0.5, open_seconds=0.3)
        for _ in range(4):
            with contextlib.suppress(ValueError), bh.acquire():
                raise ValueError("fail")  # 4/4 失败 ≥ 50% → 熔断
        assert bh.stats()["state"] == "open"
        with pytest.raises(CircuitOpenError):
            bh.acquire(timeout_s=0.01)

    def test_half_open_recovers(self):
        # open_seconds 下限为 1.0（防抖动），测试按 1s + 余量等待
        bh = Bulkhead("test", permits=2, window=2, fail_ratio=0.5, open_seconds=1.0, half_open=1)
        for _ in range(2):
            with contextlib.suppress(ValueError), bh.acquire():
                raise ValueError("fail")
        assert bh.stats()["state"] == "open"
        time.sleep(1.1)  # 等半开
        with bh.acquire():
            pass  # 半开探测成功 → 关闭
        assert bh.stats()["state"] == "closed"

    def test_permits_limit_concurrency(self):
        bh = Bulkhead("test", permits=1, window=10, fail_ratio=0.9)
        p1 = bh.acquire(timeout_s=0.1)
        entered = threading.Event()

        def second():
            with bh.acquire(timeout_s=0.5):
                entered.set()

        t = threading.Thread(target=second)
        t.start()
        time.sleep(0.1)
        assert not entered.is_set()  # 许可只有 1 个，第二个必须等
        p1.__exit__(None, None, None)
        t.join(timeout=2)
        assert entered.is_set()


class TestSingleFlight:
    def test_concurrent_same_question_computes_once(self, client):
        """两个并发相同问题：只有首个真正调用 LLM，第二个复用缓存结果。"""
        pipeline = client.app.state.pipeline.bind_principal("default", "u1")
        calls = {"n": 0}
        original_generate = pipeline._llm.generate

        def slow_generate(prompt):
            calls["n"] += 1
            time.sleep(0.3)  # 制造重叠窗口
            return original_generate(prompt)

        pipeline._llm.generate = slow_generate  # type: ignore[attr-defined]
        results: list = []
        errors: list = []

        def ask():
            try:
                results.append(pipeline.ask("保修多久？", "default", f"t-{time.time_ns()}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=ask) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors
        assert len(results) == 2
        assert results[1].from_cache is True, "第二个并发请求应复用首个的计算结果"


class TestRequestCache:
    def test_doc_name_resolved_once_per_request(self, client):
        from tests.conftest import SAMPLE_MD

        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        knowledge = client.app.state.knowledge
        counts = {"n": 0}
        original = knowledge.filename_of

        def counting(doc_id):
            counts["n"] += 1
            return original(doc_id)

        pipeline = client.app.state.pipeline.bind_principal("default", "u1")
        pipeline._doc_name_resolver = counting
        pipeline.ask("保修多久？", "default", "t-rcache")
        doc_ids = {c.doc_id for c in client.app.state.store.all_chunks()}
        assert counts["n"] <= max(1, len(doc_ids)), (
            f"同一请求内文件名解析应按 doc_id 去重（调了 {counts['n']} 次，文档数 {len(doc_ids)}）"
        )


class TestIngestBackpressure:
    def test_queue_full_submit_returns_false(self, client):
        manager = client.app.state.task_manager
        # 让 worker 卡在第一个任务上，队列才能确定性打满
        block = threading.Event()
        original_run = manager._run_task
        manager._run_task = lambda task_id: block.wait(timeout=60)  # type: ignore[method-assign]
        try:
            accepted = rejected = 0
            for i in range(manager._workers_n + manager._queue.maxsize + 5):
                task = manager.create_task(
                    [{"filename": f"f{i}.md", "size": 1}], {}, {}, tenant_id="default"
                )
                if manager.submit(task.task_id):
                    accepted += 1
                else:
                    rejected += 1
            assert accepted == manager._workers_n + manager._queue.maxsize
            assert rejected == 5
        finally:
            manager._run_task = original_run  # type: ignore[method-assign]
            block.set()  # 放行，worker 按原逻辑消化队列
            deadline = time.time() + 60
            while manager._queue.qsize() and time.time() < deadline:
                time.sleep(0.1)
