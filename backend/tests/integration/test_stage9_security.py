"""企业护栏测试。

覆盖：认证（静态凭证表/头部透传/停用账号）/ ACL 判定树（五步法顺序、deny 优先、
跨租户、默认拒绝、影响人数反查、风险分级）/ API 契约。

注：审计日志、配额、敏感词三组功能已下线，其用例与端点一并移除；
下线边界的 404 回归断言见 tests/integration/test_removal_regression.py。
"""
import pytest

from app.domain.schemas import DocumentRecord
from app.services.security.acl import ACLService
from app.services.security.auth import (
    HeaderAuthProvider,
    Principal,
    StaticTokenAuthProvider,
)
from app.core.envelope import ApiError

USERS = [
    {"user_id": "admin", "tenant_id": "default", "roles": ["admin"], "groups": [], "enabled": True},
    {"user_id": "zhangsan", "tenant_id": "default", "roles": ["hr"], "groups": [], "enabled": True},
    {"user_id": "lisi", "tenant_id": "default", "roles": ["sales"], "groups": [], "enabled": True},
    {"user_id": "wangwu", "tenant_id": "tenant-b", "roles": ["finance"], "groups": ["exec"], "enabled": True},
    {"user_id": "disabled-u", "tenant_id": "default", "roles": [], "groups": [], "enabled": False},
]


def make_doc(doc_id: str, tenant: str = "default", public: bool = False) -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id, file_id="f1", filename=f"{doc_id}.md", tenant_id=tenant,
        source_path="/tmp/x", content_hash="h", acl_public=public,
    )


class TestAuth:
    def test_header_provider_default(self):
        p = HeaderAuthProvider().authenticate(None, {"x-tenant-id": "t1", "x-user-id": "u1", "x-roles": "hr,admin"})
        assert p.tenant_id == "t1" and p.roles == ["hr", "admin"]
        assert "user:u1" in p.identities and "role:hr" in p.identities
        assert p.is_admin

    def test_static_token(self):
        provider = StaticTokenAuthProvider({
            "tok-1": {"user_id": "zhangsan", "tenant_id": "default", "roles": ["hr"]},
        })
        p = provider.authenticate("tok-1", {})
        assert p.user_id == "zhangsan"
        with pytest.raises(ApiError):
            provider.authenticate("bad-token", {})
        with pytest.raises(ApiError):
            provider.authenticate(None, {})

    def test_disabled_account_rejected(self):
        provider = StaticTokenAuthProvider({
            "tok-x": {"user_id": "disabled-u", "tenant_id": "default", "enabled": False},
        })
        with pytest.raises(ApiError, match="停用"):
            provider.authenticate("tok-x", {})


class TestACL:
    def _acl(self, tmp_path) -> ACLService:
        return ACLService(tmp_path / "acl.json", USERS)

    def test_deny_by_default(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            acl = ACLService(Path(tmp) / "acl.json", USERS)
            doc = make_doc("d1", public=False)
            zhangsan = Principal("zhangsan", "default", roles=["hr"])
            assert acl.check(zhangsan, doc) is False  # 未显式授权 → 默认拒绝

    def test_tenant_mismatch_always_denied(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            acl = ACLService(Path(tmp) / "acl.json", USERS)
            acl.set_acl("d1", allow=["role:hr"], deny=[], is_public=True, updated_by="admin")
            doc = make_doc("d1", tenant="default")
            wangwu = Principal("wangwu", "tenant-b", roles=["finance"])
            assert acl.check(wangwu, doc) is False  # 跨租户永不因公开可见

    def test_deny_overrides_allow(self):
        """deny 优先于 allow：被拉黑用户的角色授权救不回来。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            acl = ACLService(Path(tmp) / "acl.json", USERS)
            acl.set_acl("d1", allow=["role:hr"], deny=["user:zhangsan"], is_public=False, updated_by="admin")
            doc = make_doc("d1")
            assert acl.check(Principal("zhangsan", "default", roles=["hr"]), doc) is False  # 显式拉黑
            assert acl.check(Principal("other-hr", "default", roles=["hr"]), doc) is True  # 角色放行

    def test_allow_by_group(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            acl = ACLService(Path(tmp) / "acl.json", USERS)
            acl.set_acl("d1", allow=["group:exec"], deny=[], is_public=False, updated_by="admin")
            doc = make_doc("d1")
            assert acl.check(Principal("wangwu", "default", groups=["exec"]), doc) is True
            assert acl.check(Principal("lisi", "default", roles=["sales"]), doc) is False

    def test_impact_reverse_lookup(self):
        """变更影响人数由后端反查（role:hr 覆盖 zhangsan）。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            acl = ACLService(Path(tmp) / "acl.json", USERS)
            # 先置为私有（无授权），再从私有放权给 role:hr
            acl.set_acl("d1", allow=[], deny=[], is_public=False, updated_by="admin")
            result = acl.set_acl("d1", allow=["role:hr"], deny=[], is_public=False, updated_by="admin")
            assert result["impact"]["added_count"] == 1
            assert result["impact"]["added_users"] == ["zhangsan"]
            assert result["risk"] == "high"  # 扩大授权
            # 改为公开 = 最高风险
            result2 = acl.set_acl("d1", allow=[], deny=[], is_public=True, updated_by="admin")
            assert result2["risk"] == "critical"
            # 移除黑名单 = 最高风险
            acl.set_acl("d1", allow=[], deny=["user:zhangsan"], is_public=False, updated_by="admin")
            result3 = acl.set_acl("d1", allow=[], deny=[], is_public=False, updated_by="admin")
            assert result3["risk"] == "critical"


class TestSecurityApi:
    def test_users_and_acl_flow(self, client):
        # 用户列表
        users = client.get("/admin/users").json()["data"]
        assert any(u["user_id"] == "admin" for u in users)
        # ACL 列表（存量文档）
        acl_list = client.get("/admin/documents/acl").json()["data"]
        assert isinstance(acl_list, list)
        if acl_list:
            doc_id = acl_list[0]["doc_id"]
            resp = client.put(f"/admin/documents/{doc_id}/acl", json={
                "allow": ["role:hr"], "deny": [], "is_public": False,
            })
            data = resp.json()["data"]
            assert "impact" in data and "risk" in data
            assert data["impact"]["added_count"] >= 1

    def test_cannot_remove_last_admin(self, client):
        """降权最后一个管理员必须被拒——否则管理区永久失联。

        回归：降权即时生效（get_principal 复核目录），而恢复管理员的入口
        本身又要求管理员权限，于是把自己降掉就只能去改容器里的 users.json。
        """
        resp = client.put("/admin/users/admin", json={"roles": ["sales"], "groups": [], "enabled": True})
        assert resp.status_code == 409, f"应拒绝移除最后一个管理员，实际 {resp.status_code}"
        body = resp.json()
        assert body["error"]["code"] == 2001
        # 角色没被动过
        users = client.get("/admin/users").json()["data"]
        admin = next(u for u in users if u["user_id"] == "admin")
        assert "admin" in admin["roles"]

    def test_can_demote_non_admin_user(self, client):
        """非最后一个管理员被降权是允许的（守卫不能把功能整个堵死）。"""
        resp = client.put("/admin/users/zhangsan", json={"roles": ["sales"], "groups": [], "enabled": True})
        assert resp.status_code == 200
        users = client.get("/admin/users").json()["data"]
        zhangsan = next(u for u in users if u["user_id"] == "zhangsan")
        assert zhangsan["roles"] == ["sales"]
        # 还原
        client.put("/admin/users/zhangsan", json={"roles": ["hr"], "groups": [], "enabled": True})
