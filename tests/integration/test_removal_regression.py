"""下线回归验收：对 TestClient 跑真实 HTTP，验证删除边界。

分三类断言：
1. 已删端点 → 404（且错误信封格式仍正确，说明 404 处理器未受影响）
2. 保留端点 → 200/预期状态（尤其 /admin/costs 与监控大盘相关端点）
3. 已下线的认证/授权端点 → 404，且**不得**削弱任何保留端点

**认证与授权下线（2026-11-12）**：本文件新增了「认证授权端点已下线」一组断言——
注册/登录/当前身份、用户与角色管理、文档 ACL、知识库授权与权限矩阵端点必须 404。
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

    def test_admin_costs_no_longer_role_gated(self, client):
        """角色门禁已下线：管理面端点不再按角色拦截。

        访问边界改由部署层承担（反向代理 / 内网 / 网关）。
        """
        resp = client.get("/admin/costs", headers={"X-User-Id": "alice", "X-Roles": ""})
        assert resp.status_code == 200, f"角色门禁已下线，不应被拦截：{resp.status_code}"

    def test_admin_costs_invalid_group_by(self, client):
        """白名单约束不得因迁移丢失（不得任意取 CostRecord 属性）。"""
        resp = client.get("/admin/costs", params={"group_by": "password"})
        # 本项目把参数校验失败统一映射为 400（见 test_tools_extra_and_obs 同款断言）
        assert resp.status_code in (400, 422), f"group_by 应受白名单约束，实得 {resp.status_code}"

    @pytest.mark.parametrize("path", [
        "/health", "/healthz", "/livez", "/readyz",
        "/admin/metrics/summary", "/admin/metrics/agent", "/admin/metrics/quality",
        "/admin/metrics/timeseries", "/admin/metrics/top-queries",
        "/admin/tools",
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



class TestAuthAndPermissionEndpointsRemoved:
    """认证与授权端点必须彻底消失（2026-11-12 下线）。

    这组能力是"主动下线"，边界同样要守住：端点必须 404、错误信封必须仍然正确，
    且**不得**削弱任何保留端点。
    """

    @pytest.mark.parametrize("path", [
        "/auth/login",
        "/auth/register",
        "/auth/me",
        "/admin/users",
        "/admin/users/admin",
        "/admin/users/admin/password",
        "/admin/documents/acl",
        "/admin/documents/doc-x/acl",
        "/admin/users/permission-matrix",
        "/admin/kb",
        "/admin/kb/default",
        "/admin/kb/default/grants",
        "/admin/users/admin/kbs",
    ])
    def test_read_endpoints_return_404_with_envelope(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 404, f"{path} 应 404，实得 {resp.status_code}"
        body = resp.json()
        assert body.get("status") == "error"
        assert body.get("error", {}).get("message")

    def test_write_methods_also_gone(self, client):
        assert client.post("/auth/login", json={"username": "a", "password": "b"}).status_code == 404
        assert client.post("/auth/register", json={"username": "a", "password": "b"}).status_code == 404
        assert client.put("/admin/users/admin", json={"roles": []}).status_code == 404
        assert client.put("/admin/users/admin/password", json={"password": "x"}).status_code == 404
        assert client.put("/admin/documents/doc-x/acl", json={}).status_code == 404
        assert client.post("/admin/kb", json={"name": "k"}).status_code == 404

    def test_removed_paths_absent_from_openapi(self, client):
        spec = client.app.openapi()["paths"]
        for path in ("/auth/login", "/auth/register", "/auth/me",
                     "/admin/users", "/admin/users/{user_id}",
                     "/admin/users/{user_id}/password",
                     "/admin/documents/acl", "/admin/documents/{doc_id}/acl",
                     "/admin/users/permission-matrix",
                     "/admin/kb", "/admin/kb/{kb_id}", "/admin/kb/{kb_id}/grants",
                     "/admin/users/{user_id}/kbs"):
            assert path not in spec, f"{path} 不应再出现在 OpenAPI 契约里"

    def test_auth_modules_removed_from_codebase(self):
        """认证模块必须真的删掉（不留"半残实现"骗过路由层）。"""
        import importlib

        for mod in ("app.services.security.auth",
                    "app.services.security.password",
                    "app.services.security.acl",
                    "app.services.security.kb"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(mod)

    def test_ask_path_still_works_without_acl(self, client):
        """删除权限判定不得波及问答主链路。"""
        resp = client.post("/ask", json={"question": "保修多久？", "mode": "auto"})
        assert resp.status_code == 200
        assert resp.json()["status"] in ("ok", "warn")


class TestAskAndAgentPathUnaffected:
    """ACL 收口与取消标志等被报告中心共用的能力不能受影响。"""

    def test_acl_filter_dependency_removed(self):
        """acl_filter_of 随文档级 ACL 一并删除（依赖里不得再有残留）。"""
        import app.dependencies as deps
        assert not hasattr(deps, "acl_filter_of")
        assert not hasattr(deps, "require_role")

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

