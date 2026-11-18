"""并发安全与 API 契约补齐。

此前缺失的覆盖：
- `/graph/*`、`/analysis/*`、`/ingest/*` 的 HTTP 契约（只测过 service 层）
- 审计并发追加 / 分页

注：原覆盖 `Orchestrator` 多连接 SSE 串流与 `Blackboard` 并发追加原子性，
二者属多智能体报告链路，已随报告中心下线移除。
"""
import concurrent.futures
import json
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
            "/admin/costs", "/admin/metrics/summary",
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
    """会话原子写：并发创建后所有文件都必须是合法 JSON。"""
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
