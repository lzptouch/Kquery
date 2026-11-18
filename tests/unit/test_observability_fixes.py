"""可观测四坑 + 失败语义的回归测试（X-04 / X-15 / X-16 / X-18）。

这四项的共同特征：**改之前"看起来是好的"**——
字段结构在但内容空、桶在但量纲不对、探针在但会误杀 Pod、错误在但状态码是 200。
所以测试必须断言"具体行为"，而不是"有这个东西"。
"""
import logging

from app.core.logging_setup import ContextFilter
from app.core.tracing import (
    get_request_context,
    reset_request_context,
    set_request_context,
)


class TestRequestContext:
    def test_context_fields_are_readable(self):
        token = set_request_context(tenant_id="t1", user_id="u1", route="/ask")
        try:
            ctx = get_request_context()
            assert ctx["tenant_id"] == "t1"
            assert ctx["user_id"] == "u1"
            assert ctx["route"] == "/ask"
        finally:
            reset_request_context(token)

    def test_context_is_isolated_per_call(self):
        token = set_request_context(tenant_id="t1")
        try:
            assert get_request_context()["tenant_id"] == "t1"
        finally:
            reset_request_context(token)


class TestContextFilter:
    def _record(self, **extra):
        record = logging.LogRecord("x", logging.INFO, "f.py", 1, "msg", (), None)
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_tenant_route_latency_are_injected(self):
        """X-15：这些字段此前"结构对了、内容空"——现在必须有内容。"""
        token = set_request_context(
            tenant_id="t-9", user_id="u-9", route="/ask", latency_ms=42
        )
        try:
            record = self._record()
            ContextFilter().filter(record)
            assert record.tenant_id == "t-9"
            assert record.user_id == "u-9"
            assert record.route == "/ask"
            assert record.latency_ms == 42
        finally:
            reset_request_context(token)

    def test_explicit_extra_wins(self):
        """业务代码显式 extra 的值优先（不能被上下文覆盖）。"""
        token = set_request_context(tenant_id="ctx-tenant")
        try:
            record = self._record(tenant_id="explicit")
            ContextFilter().filter(record)
            assert record.tenant_id == "explicit"
        finally:
            reset_request_context(token)

    def test_missing_context_does_not_raise(self):
        record = self._record()
        ContextFilter().filter(record)  # 不抛即可
        # 上面的用例可能留下了上下文，字段有值也算通过（重点是"不抛"）


class TestMetricsFixes:
    def test_latency_buckets_cover_long_requests(self):
        """X-16：桶上限必须覆盖业务真实时长（SSE 可跑 60s+）。"""
        from app.core.metrics import PromMetrics

        buckets = PromMetrics().latency_seconds._upper_bounds
        assert max(buckets) >= 120.0
        assert 30.0 in buckets and 60.0 in buckets

    def test_top_score_buckets_align_with_crag_thresholds(self):
        """X-16：桶要覆盖 CRAG 阈值（0.08/0.14），否则合格分数全在最后一桶。"""
        from app.core.metrics import PromMetrics

        buckets = PromMetrics().retrieval_top_score._upper_bounds
        assert max(buckets) >= 0.5
        assert 0.08 in buckets and 0.14 in buckets

    def test_top_queries_is_bounded(self):
        """X-16：top queries 必须有界（此前无界 dict → 内存泄漏）。"""
        from app.core.metrics import MetricsStore

        store = MetricsStore()
        for i in range(1200):
            store.record_request(f"问题 {i}", True, 10.0, False, True)
        assert len(store._top_queries) <= store._top_queries_capacity

    def test_top_queries_keeps_hot_entries(self):
        """LRU：热问题留在容量内，冷问题被淘汰（键是哈希，不存原文）。"""
        from app.core.metrics import MetricsStore

        store = MetricsStore()
        for i in range(600):
            store.record_request(f"冷问题 {i}", True, 10.0, False, True)
        for _ in range(5):
            store.record_request("热问题", True, 10.0, False, True)
        hot = store._query_fingerprint("热问题")
        assert hot in store._top_queries
        assert store._top_queries[hot]["count"] == 5


class TestProbeHelpers:
    def test_parallel_probe_reports_all(self):
        from app.main import _probe_all

        probes = [
            ("a", lambda: {"x": 1}),
            ("b", lambda: {"y": 2}),
            ("c", lambda: (_ for _ in ()).throw(RuntimeError("boom"))),
        ]
        deps = _probe_all(probes, timeout_s=1.0)
        assert len(deps) == 3
        by_name = {d["name"]: d for d in deps}
        assert by_name["a"]["status"] == "up"
        assert by_name["c"]["status"] == "down"

    def test_slow_probe_times_out_without_blocking(self):
        """X-18：慢依赖不得把整个探针拖长（否则 K8s probe timeout 误杀 Pod）。"""
        import time

        from app.main import _probe_all

        def slow():
            time.sleep(5)
            return {}

        deps = _probe_all([("slow", slow)], timeout_s=0.2)
        assert deps[0]["status"] == "down"
        assert deps[0]["error"] == "Timeout"

    def test_error_does_not_leak_message(self):
        """X-18：错误只回类型，不回原文（连接串/路径不该出现在探针响应里）。"""
        from app.main import _probe_all

        def boom():
            raise RuntimeError("redis://user:secret@10.0.0.1:6379/0")

        deps = _probe_all([("x", boom)], timeout_s=1.0)
        assert deps[0]["status"] == "down"
        assert "secret" not in str(deps[0])
        assert deps[0]["error"] == "RuntimeError"


class TestFailureSemantics:
    def test_llm_error_status_mapping(self):
        from app.api.ask import _llm_error_status
        from app.core.envelope import ErrCode

        assert _llm_error_status(int(ErrCode.GENERATION_TIMEOUT)) == 504
        assert _llm_error_status(int(ErrCode.GENERATION_FAILED)) == 502
        assert _llm_error_status(int(ErrCode.RATE_LIMITED)) == 429
        assert _llm_error_status(int(ErrCode.DEPENDENCY_UNAVAILABLE)) == 503
        assert _llm_error_status(99999) == 502

    def test_unknown_route_is_not_reported_as_document_not_found(self, client):
        """X-04：URL 拼错不是"文档不存在"。"""

        r = client.get("/ask/no-such-endpoint")
        assert r.status_code == 404
        assert r.json()["error"]["code"] != 3002
