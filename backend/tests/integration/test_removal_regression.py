"""下线回归验收：对 TestClient 跑真实 HTTP，验证删除边界。

分三类断言：
1. 已删端点 → 404（且错误信封格式仍正确，说明 404 处理器未受影响）
2. 保留端点 → 200/预期状态（尤其 /admin/costs 与监控大盘相关端点）
3. 管理面鉴权矩阵仍然生效（删除未削弱任何门禁）
"""
import pytest


class TestRemovedEndpointsAreGone:
    """报告中心 + 评测平台端点必须彻底消失。"""

    @pytest.mark.parametrize("path", [
        "/reports",
        "/reports/r1",
        "/reports/r1/plan",
        "/reports/r1/evidence",
        "/reports/r1/critique",
        "/reports/r1/stream",
        "/admin/eval/datasets",
        "/admin/eval/runs",
        "/admin/eval/runs/x",
        "/admin/eval/compare",
        "/admin/metrics/multiagent",
    ])
    def test_returns_404_with_envelope(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 404, f"{path} 应 404，实得 {resp.status_code}"
        body = resp.json()
        # 404 必须仍走统一信封（说明 http_error_handler 未被破坏）
        assert body.get("status") == "error"
        assert body.get("error", {}).get("message")

    def test_write_methods_also_gone(self, client):
        assert client.post("/reports", json={"goal": "x" * 10}).status_code == 404
        assert client.post("/admin/eval/run", json={"dataset": "d", "dataset_version": "v"}).status_code == 404
        assert client.post("/admin/eval/datasets", json={"name": "n", "version": "v", "samples": []}).status_code == 404
        assert client.delete("/reports/r1").status_code == 404


class TestPreservedEndpointsStillWork:
    """删除不得波及任何其他功能。"""

    def test_admin_costs_survived(self, client):
        """ 成本看板原寄居 reports.py，必须迁出后继续可用。"""
        resp = client.get("/admin/costs")
        assert resp.status_code == 200, f"成本看板失效：{resp.status_code} {resp.text[:200]}"
        data = resp.json()["data"]
        assert "top_traces" in data
        assert "groups" in data or "by_phase" in data or data  # 聚合结构存在即可

    def test_admin_costs_still_admin_only(self, client):
        """迁移后门禁不能丢。"""
        resp = client.get("/admin/costs", headers={"X-User-Id": "alice", "X-Roles": ""})
        assert resp.status_code == 403

    def test_admin_costs_invalid_group_by(self, client):
        """白名单约束不得因迁移丢失（不得任意取 CostRecord 属性）。"""
        resp = client.get("/admin/costs", params={"group_by": "password"})
        # 本项目把参数校验失败统一映射为 400（见 test_tools_extra_and_obs 同款断言）
        assert resp.status_code in (400, 422), f"group_by 应受白名单约束，实得 {resp.status_code}"

    @pytest.mark.parametrize("path", [
        "/health", "/healthz", "/livez", "/readyz",
        "/admin/metrics/summary", "/admin/metrics/agent", "/admin/metrics/quality",
        "/admin/metrics/timeseries", "/admin/metrics/top-queries",
        "/admin/tools", "/admin/users", "/admin/documents/acl",
        "/admin/users/permission-matrix",
        "/admin/retrieval-config",
        "/sessions", "/graph/stats", "/knowledge/stats",
    ])
    def test_untouched_admin_and_core_endpoints(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} 应仍可用，实得 {resp.status_code}: {resp.text[:160]}"

    def test_monitor_dashboard_data_sources(self, client):
        """MonitorPage 的每一路数据源都必须还在（R2 回归防护）。"""
        for path in ("/admin/metrics/summary", "/admin/metrics/timeseries?metric=requests",
                     "/admin/metrics/top-queries", "/admin/metrics/quality",
                     "/admin/metrics/agent", "/graph/stats"):
            assert client.get(path).status_code == 200, f"监控大盘数据源 {path} 失效"

    def test_permission_matrix_no_run_eval(self, client):
        data = client.get("/admin/users/permission-matrix").json()["data"]
        caps = {c["key"] for c in data["capabilities"]}
        assert "run_eval" not in caps
        # 其余能力必须完好
        assert {"ask", "manage_acl", "manage_users",
                "view_all_tenant_data"} <= caps
        # 已下线能力不得残留在矩阵里
        assert not ({"manage_quota", "view_audit", "manage_sensitive"} & caps)


class TestAskAndAgentPathUnaffected:
    """ACL 收口与取消标志等被报告中心共用的能力不能受影响。"""

    def test_acl_filter_dependency_still_importable(self):
        """acl_filter_of 曾被 ask/reports 共用；报告删除后 ask 仍须可用。"""
        from app.dependencies import acl_filter_of
        assert callable(acl_filter_of)

    def test_orchestrator_dependency_removed(self):
        import app.dependencies as deps
        assert not hasattr(deps, "get_orchestrator")

    def test_ask_stream_endpoint_exists(self, client):
        spec = client.app.openapi()["paths"]
        assert "/ask/stream" in spec and "/ask/runs/{run_id}/cancel" in spec


class TestSecurityFeaturesRemoved:
    """敏感词 / 配额 / 审计日志三组端点必须彻底消失。

    这三组功能是"主动下线"（不同于报告中心的"误删修复"），但边界同样要守住：
    端点必须 404、错误信封必须仍然正确、且**不得**削弱任何保留端点。
    """

    @pytest.mark.parametrize("path", [
        "/admin/audit",
        "/admin/audit/export",
        "/admin/quota",
        "/admin/quota/platform",
        "/admin/sensitive",
    ])
    def test_read_endpoints_return_404_with_envelope(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 404, f"{path} 应 404，实得 {resp.status_code}"
        body = resp.json()
        # 404 必须仍走统一信封（说明 http_error_handler 未被破坏）
        assert body.get("status") == "error"
        assert body.get("error", {}).get("message")

    def test_write_methods_also_gone(self, client):
        assert client.put("/admin/quota", json={"tenant_id": "default",
                                                 "per_minute": 5, "per_day": 100}).status_code == 404
        assert client.post("/admin/sensitive", json={"word": "x", "category": "机密",
                                                      "action": "mask"}).status_code == 404
        assert client.delete("/admin/sensitive/x").status_code == 404

    def test_removed_paths_absent_from_openapi(self, client):
        spec = client.app.openapi()["paths"]
        for path in ("/admin/audit", "/admin/audit/export", "/admin/quota",
                     "/admin/quota/platform", "/admin/sensitive"):
            assert path not in spec, f"{path} 不应再出现在 OpenAPI 契约里"

    def test_graph_backend_preserved(self, client):
        """图谱只下线前端展示，后端图谱能力必须保留（问答仍靠它增强检索）。"""
        resp = client.get("/graph/stats")
        assert resp.status_code == 200, f"后端图谱统计应保留：{resp.status_code}"
        assert "images" in resp.json()["data"]

    def test_login_rate_limit_endpoint_still_present(self, client):
        """登录限流是独立实现（独立令牌桶），不随配额下线而消失。

        端到端的限流行为由 tests/integration/test_auth_flow.py 的
        test_repeated_failures_are_throttled 覆盖（需要账号密码档的 client，
        与这里的头部透传档不是同一条代码路径）。
        """
        spec = client.app.openapi()["paths"]
        assert "/auth/login" in spec, "登录端点必须保留"
        assert "/auth/register" in spec, "注册端点必须保留"
