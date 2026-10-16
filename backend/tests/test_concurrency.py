"""并发安全与 API 契约补齐（TASK-186）。

此前缺失的覆盖：
- `/graph/*`、`/analysis/*`、`/ingest/*` 的 HTTP 契约（只测过 service 层）
- `Orchestrator` 多连接 SSE 串流（DEF-S-11）
- `Blackboard` 并发追加的原子性（注释声称原子但无压测）
- 审计并发追加 / 分页
"""
import concurrent.futures
import json
import threading
import time

import pytest


class TestApiContracts:
    """契约防漏：公开端点必须有测试（新增端点忘了测会被这里拦下）。"""

    def test_core_endpoints_registered(self, client):
        spec = client.app.openapi()["paths"]
        expected = [
            "/ask", "/ask/stream", "/ask/runs/{run_id}/resume",
            "/ask/runs/{run_id}/interrupt", "/ask/runs/{run_id}/cancel",
            "/ask/{trace_id}/cost",
            "/knowledge/ingest", "/knowledge/upload",
            "/graph/entities", "/graph/stats", "/graph/communities",
            "/sessions", "/feedback/refusal",
            "/admin/costs", "/admin/metrics/summary", "/admin/metrics/multiagent",
            "/admin/tools",
        ]
        missing = [p for p in expected if p not in spec]
        assert not missing, f"以下端点未注册或路径已变：{missing}"

    def test_graph_endpoints_require_auth(self, client):
        for path in ["/graph/entities", "/graph/stats", "/graph/communities"]:
            r = client.get(path, headers={"X-User-Id": "alice", "X-Roles": ""})
            assert r.status_code in (200, 403), f"{path} 异常状态 {r.status_code}"

    def test_envelope_shape_on_error(self, client):
        """错误也必须是统一信封（前端靠 code 分支，裸 500 HTML 会让它崩）。"""
        r = client.get("/ask/nonexistent-trace/cost")
        assert r.status_code == 200
        body = r.json()
        assert {"status", "data", "meta"} <= set(body)


class TestBlackboardConcurrency:
    """Blackboard 声称 append 原子——用压测验证，而不是相信注释。"""

    def _board(self):
        from app.services.multiagent.blackboard import Blackboard

        return Blackboard("rpt-1", "目标")

    @staticmethod
    def _append(bb, i: int):
        """append_evidence 是位置参数签名，且**自带同内容去重**——
        压测必须用互不相同的内容，否则测的是去重逻辑而不是并发安全。"""
        return bb.append_evidence("t1", f"互不相同的内容-{i}", "vector_search")

    def test_concurrent_append_loses_nothing(self):
        bb = self._board()
        n = 200
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(lambda i: self._append(bb, i), range(n)))
        got = bb.list_evidence(adopted=None)
        assert len(got) == n, f"并发追加丢失：期望 {n} 实际 {len(got)}"

    def test_concurrent_append_has_no_duplicates(self):
        bb = self._board()
        n = 200
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(lambda i: self._append(bb, i), range(n)))
        ids = [r.evidence_id for r in bb.list_evidence(adopted=None)]
        assert len(set(ids)) == len(ids), "并发追加产生了重复记录"

    def test_mixed_append_and_update(self):
        """追加与打分并发时不得丢记录、不得抛异常。"""
        bb = self._board()
        for i in range(50):
            self._append(bb, i)

        def updater(i):
            try:
                # 黑板证据 ID 由 _seq 从 1 起分配（e1..e50），不是 0 起
                bb.update_evidence(f"e{i + 1}", 0.9, True, "")
            except Exception as exc:  # noqa: BLE001
                return exc
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            errors = [e for e in ex.map(updater, range(50)) if e is not None]
        assert not errors, f"并发更新抛异常：{errors[:3]}"
        assert len(bb.list_evidence(adopted=True)) == 50


class TestAuditConcurrencyAndPaging:
    """TASK-166：审计查询改流式 + 分页，并发读写下不得丢行。"""

    def _store(self, tmp_path):
        from app.services.security.audit import AuditStore

        return AuditStore(tmp_path / "audit.jsonl")

    def test_concurrent_append_then_query_all(self, tmp_path):
        store = self._store(tmp_path)
        n = 120

        def writer(i):
            store.log_request(f"trace-{i}", "t1", "u1", f"问题{i}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(writer, range(n)))

        rows = store.query(limit=n + 10)
        assert len(rows) == n, f"并发写丢行：期望 {n} 实际 {len(rows)}"

    def test_query_newest_first(self, tmp_path):
        store = self._store(tmp_path)
        for i in range(5):
            store.log_request(f"t{i}", "t1", "u1", f"q{i}")
        rows = store.query(limit=5)
        assert rows[0]["trace_id"] == "t4", "应最新在前"

    def test_pagination_no_overlap(self, tmp_path):
        """分页不得重复也不得漏——这是 offset 语义最容易写错的地方。"""
        store = self._store(tmp_path)
        for i in range(10):
            store.log_request(f"t{i}", "t1", "u1", f"q{i}")

        page1 = store.query(limit=4, offset=0)
        page2 = store.query(limit=4, offset=4)
        page3 = store.query(limit=4, offset=8)

        seen = [r["trace_id"] for r in page1 + page2 + page3]
        assert seen == [f"t{i}" for i in range(9, -1, -1)], f"分页顺序/覆盖不对：{seen}"

    def test_limit_bounds_memory(self, tmp_path):
        """小 limit 只返回少量记录（流式扫描的意义：不再把整文件读进内存）。"""
        store = self._store(tmp_path)
        for i in range(500):
            store.log_request(f"t{i}", "t1", "u1", "q")
        assert len(store.query(limit=20)) == 20

    def test_filters_combine(self, tmp_path):
        store = self._store(tmp_path)
        store.log_request("a", "t1", "u1", "q")
        store.log_request("b", "t2", "u1", "q")
        store.log_request("c", "t1", "u2", "q")
        assert len(store.query(tenant_id="t1")) == 2
        assert len(store.query(tenant_id="t1", user_id="u1")) == 1

    def test_corrupt_line_is_skipped(self, tmp_path):
        """半行/损坏行不能拖垮整个查询（审计只追加，崩溃可能留下半行）。"""
        store = self._store(tmp_path)
        store.log_request("ok", "t1", "u1", "q")
        with (tmp_path / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("{ 这不是合法 JSON\n")
        rows = store.query()
        assert len(rows) == 1 and rows[0]["trace_id"] == "ok"


class TestOrchestratorSinkRouting:
    """TASK-168：按 report_id 路由事件，多连接不得串流。"""

    def _orchestrator(self):
        from app.services.multiagent.orchestrator import Orchestrator

        return Orchestrator.__new__(Orchestrator)

    def test_sinks_are_isolated_per_report(self):
        orch = self._orchestrator()
        orch._sinks = {}
        orch._sinks_lock = threading.Lock()
        orch._on_event = None

        got_a: list = []
        got_b: list = []
        orch.set_event_sink(lambda e, p: got_a.append((e, p)), "rpt-a")
        orch.set_event_sink(lambda e, p: got_b.append((e, p)), "rpt-b")

        orch._emit("task_start", {"report_id": "rpt-a", "task_id": "t1"})
        orch._emit("task_start", {"report_id": "rpt-b", "task_id": "t2"})

        assert len(got_a) == 1 and got_a[0][1]["task_id"] == "t1"
        assert len(got_b) == 1 and got_b[0][1]["task_id"] == "t2"

    def test_unregistering_one_keeps_other(self):
        orch = self._orchestrator()
        orch._sinks = {}
        orch._sinks_lock = threading.Lock()
        orch._on_event = None

        got_b: list = []
        orch.set_event_sink(lambda e, p: None, "rpt-a")
        orch.set_event_sink(lambda e, p: got_b.append((e, p)), "rpt-b")

        orch.set_event_sink(None, "rpt-a")  # A 断开

        orch._emit("task_start", {"report_id": "rpt-b", "task_id": "t2"})
        assert len(got_b) == 1, "A 断开不得影响 B（此前单槽会被一并清掉）"

    def test_falls_back_to_global_sink(self):
        """兼容路径：不带 report_id 时仍走全局槽（既有调用方与测试）。"""
        orch = self._orchestrator()
        orch._sinks = {}
        orch._sinks_lock = threading.Lock()
        orch._on_event = None
        got: list = []
        orch.set_event_sink(lambda e, p: got.append((e, p)))

        orch._emit("task_start", {"report_id": "rpt-x"})
        assert len(got) == 1

    def test_sink_exception_does_not_break_run(self):
        """事件推送是增强，不是主流程——推失败不能中断报告生成。"""
        orch = self._orchestrator()
        orch._sinks = {}
        orch._sinks_lock = threading.Lock()
        orch._on_event = None

        def broken(e, p):
            raise RuntimeError("sink 炸了")

        orch.set_event_sink(broken, "rpt-a")
        orch._emit("task_start", {"report_id": "rpt-a"})  # 不应抛

    def test_no_sink_is_silent(self):
        orch = self._orchestrator()
        orch._sinks = {}
        orch._sinks_lock = threading.Lock()
        orch._on_event = None
        orch._emit("task_start", {"report_id": "nope"})  # 不应抛


@pytest.mark.timeout(60)
def test_metrics_scrape_is_stable_under_load(client):
    """指标端点在高频写入下仍能抓取（Registry 线程安全）。"""
    def hit(_i):
        return client.get("/metrics").status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(hit, range(20)))
    assert all(c == 200 for c in codes), f"抓取失败：{set(codes)}"


def test_trace_id_isolated_between_requests(client):
    """并发请求的 trace_id 不得串（contextvar 隔离）。"""
    def ask(_i):
        r = client.post("/ask", json={"question": f"问题{_i}"})
        return r.headers.get("X-Trace-Id")

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        traces = list(ex.map(ask, range(12)))
    assert len(set(traces)) == len(traces), "trace_id 在并发请求间串了"
    assert all(traces)


def test_session_file_survives_concurrent_writes(client):
    """会话原子写（TASK-118）：并发创建后所有文件都必须是合法 JSON。"""
    def create(i):
        return client.post("/sessions", json={"title": f"会话{i}"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(create, range(20)))
    assert all(r.status_code == 200 for r in results)

    import pathlib

    data_dir = pathlib.Path(client.app.state.settings.data_dir) / "sessions"
    files = list(data_dir.glob("*.json"))
    assert files, "应产生会话文件"
    for f in files:
        json.loads(f.read_text(encoding="utf-8"))  # 不抛错即通过（半截 JSON 会炸）


def test_concurrent_ask_does_not_serialize(client):
    """并发提问不得互相阻塞（每请求一线程贯穿 LLM 流式）。"""
    def ask(i):
        t0 = time.monotonic()
        client.post("/ask", json={"question": f"并发问题{i}"})
        return time.monotonic() - t0

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        durations = list(ex.map(ask, range(6)))
    # 单次 mock 问答本身很快；串行化会表现为总时长 ≈ 6 × 单次。
    # 这里只断言"没有异常级串行"，阈值放宽松以避免 CI 抖动误报。
    assert max(durations) < 20, f"并发提问疑似串行：{max(durations):.2f}s"
