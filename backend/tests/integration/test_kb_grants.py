"""知识库授权端到端：建库 → 移入文档 → 授权 → 检索可见性 → 撤权 → 缓存失效。

要解决的问题：KB 授权不是"建了一张表"而是"检索结果真的变了"。
每个用例都走真实 HTTP + 真实检索链路（client fixture：临时数据目录 + mock 模型）。

安全守护：T-I13/T-I14 是降级路径的越权守护——下推 provider 失效时，
KB 闸门必须由后过滤闭包兜底，否则私有库文档经「tenant 粗过滤 + ACL 恒放行」漏出。
"""
from tests.conftest import SAMPLE_MD

LISI = {"X-User-Id": "lisi", "X-Tenant-Id": "default", "X-Roles": "sales"}


def _upload(client, name="产品手册.md", content=SAMPLE_MD, kb_id=None):
    data = {"file": (name, content.encode("utf-8"), "text/markdown")}
    form = {} if kb_id is None else {"kb_id": kb_id}
    resp = client.post("/knowledge/upload", files=data, data=form)
    assert resp.status_code == 200, resp.json()
    return resp.json()["data"]


def _ask(client, question, headers):
    resp = client.post("/ask", json={"question": question, "mode": "auto"}, headers=headers)
    assert resp.status_code == 200
    return resp.json()["data"]


class TestKbLifecycle:
    def test_default_kb_created_and_public(self, client):
        """首次启动自动创建默认 KB（承接存量文档，升级不回归）。"""
        data = client.get("/admin/kb").json()["data"]
        default = next(k for k in data if k["kb_id"] == "default")
        assert default["is_public"] is True
        assert default["name"] == "默认知识库"

    def test_upload_defaults_to_default_kb(self, client):
        data = _upload(client)
        assert data["knowledge_base_id"] == "default"

    def test_upload_with_explicit_kb(self, client):
        created = client.post("/admin/kb", json={"name": "财务制度库", "is_public": False}).json()["data"]
        data = _upload(client, kb_id=created["kb_id"])
        assert data["knowledge_base_id"] == created["kb_id"]

    def test_upload_to_unknown_kb_rejected(self, client):
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("a.md", SAMPLE_MD.encode(), "text/markdown")},
            data={"kb_id": "不存在"},
        )
        assert resp.status_code == 404, "不存在的 KB 应拒绝（防探测口径）"
        assert "无权限或不存在" in resp.json()["error"]["message"]

    def test_non_admin_cannot_manage_kb(self, client):
        for method, path in (("get", "/admin/kb"), ("post", "/admin/kb")):
            resp = getattr(client, method)(path, headers=LISI)
            assert resp.status_code == 403, f"{method} {path} 非管理员应 403"
        assert "无权限或不存在" in client.get("/admin/kb", headers=LISI).json()["error"]["message"]


class TestKbAuthorizationEndToEnd:
    """核心链路：私有库 + 未授权用户不可见，授权后可见。"""

    def _setup_private_kb_with_doc(self, client):
        created = client.post("/admin/kb", json={"name": "机密库", "is_public": False}).json()["data"]
        uploaded = _upload(client)
        doc_id = uploaded["doc_id"]
        moved = client.put(f"/admin/kb/{created['kb_id']}/documents", json={"doc_ids": [doc_id]})
        assert moved.status_code == 200
        assert moved.json()["data"]["moved"] == [doc_id]
        return created["kb_id"], doc_id

    def test_ungranted_user_cannot_see_private_kb_docs(self, client):
        """文档在私有库 + lisi 未授权：ask 不得命中该文档的证据。"""
        self._setup_private_kb_with_doc(client)
        data = _ask(client, "支持哪些文档格式？", LISI)
        assert data["evidence"] == [], "未授权用户不得看到私有库文档的证据"
        assert data["verdict"] == "refused"

    def test_granted_user_sees_private_kb_docs(self, client):
        kb_id, _ = self._setup_private_kb_with_doc(client)
        granted = client.put(f"/admin/kb/{kb_id}/grants", json={"user_ids": ["lisi"]})
        assert granted.status_code == 200
        assert granted.json()["data"]["impact"]["added_count"] == 1
        data = _ask(client, "支持哪些文档格式？", LISI)
        assert data["evidence"], "授权后必须能检索到私有库文档"

    def test_revocation_takes_effect_immediately(self, client):
        """撤权即失效：缓存签名携带 KB 复合版本，旧答案不得再命中。"""
        kb_id, _ = self._setup_private_kb_with_doc(client)
        client.put(f"/admin/kb/{kb_id}/grants", json={"user_ids": ["lisi"]})
        assert _ask(client, "支持哪些文档格式？", LISI)["evidence"]
        client.put(f"/admin/kb/{kb_id}/grants", json={"user_ids": []})
        data = _ask(client, "支持哪些文档格式？", LISI)
        assert data["evidence"] == [], "撤权后旧证据不得再出现（缓存必须随 KB 版本失效）"

    def test_acl_deny_wins_over_kb_grant(self, client):
        """deny 优先：KB 已授权但文档 ACL 拉黑 → 仍不可见（判定树顺序）。"""
        kb_id, doc_id = self._setup_private_kb_with_doc(client)
        client.put(f"/admin/kb/{kb_id}/grants", json={"user_ids": ["lisi"]})
        client.put(f"/admin/documents/{doc_id}/acl",
                   json={"allow": [], "deny": ["user:lisi"], "is_public": False})
        data = _ask(client, "支持哪些文档格式？", LISI)
        assert data["evidence"] == [], "KB 授权不能救回被文档 ACL 拉黑者"


class TestKbRetrievalDegradationGuards:
    """降级路径的越权守护：下推 provider 失效时，KB 必须由后过滤兜底。"""

    def test_kb_enforced_when_pushdown_provider_degraded(self, client):
        """下推退化（provider 不可用）≠ 权限失效：后过滤闭包仍查 KB。"""
        created = client.post("/admin/kb", json={"name": "机密库", "is_public": False}).json()["data"]
        uploaded = _upload(client)
        client.put(f"/admin/kb/{created['kb_id']}/documents", json={"doc_ids": [uploaded["doc_id"]]})
        # 强制下推退化：撤掉 pipeline 的 ACL 依赖，bind_principal 建不出 provider
        pipeline = client.app.state.pipeline
        saved = pipeline._acl_service
        pipeline._acl_service = None
        try:
            data = _ask(client, "支持哪些文档格式？", LISI)
            assert data["evidence"] == [], (
                "下推 provider 退化时，后过滤闭包必须仍拦私有库文档"
            )
            assert data["verdict"] == "refused"
        finally:
            pipeline._acl_service = saved

    def test_kb_disabled_reverts_to_acl_only(self, client):
        """对照组：kb_enabled=false 时同样降级，私有库可见（证明上一条确实依赖 KB 开关，
        排除「本来就检不到」的假通过）。"""
        created = client.post("/admin/kb", json={"name": "机密库", "is_public": False}).json()["data"]
        uploaded = _upload(client)
        client.put(f"/admin/kb/{created['kb_id']}/documents", json={"doc_ids": [uploaded["doc_id"]]})
        settings = client.app.state.settings
        settings.kb_enabled = False
        try:
            data = _ask(client, "支持哪些文档格式？", LISI)
            assert data["evidence"], "kb_enabled=false 必须退回纯 ACL 行为（默认公开）"
        finally:
            settings.kb_enabled = True


class TestKbAuditAndMatrix:
    def test_grant_writes_audit_with_impact(self, client):
        created = client.post("/admin/kb", json={"name": "制度库", "is_public": False}).json()["data"]
        client.put(f"/admin/kb/{created['kb_id']}/grants", json={"user_ids": ["lisi"]})
        events = client.get("/admin/audit", params={"range_s": 600, "event": "admin.action"}).json()["data"]
        grants = [e for e in events if e["payload"].get("action") == "kb.grant.set"]
        assert grants, "授权操作必须写审计"
        assert grants[0]["payload"]["added"] == 1

    def test_permission_matrix_includes_manage_kb(self, client):
        data = client.get("/admin/users/permission-matrix").json()["data"]
        caps = {c["key"] for c in data["capabilities"]}
        assert "manage_kb" in caps
        admin_row = next(r for r in data["matrix"] if r["role"] == "admin")
        assert admin_row["capabilities"]["manage_kb"] is True


class TestKbDeletionSafety:
    def test_default_kb_cannot_be_deleted(self, client):
        resp = client.delete("/admin/kb/default")
        assert resp.status_code == 400
        assert "默认" in resp.json()["error"]["message"]

    def test_non_empty_kb_cannot_be_deleted(self, client):
        created = client.post("/admin/kb", json={"name": "制度库", "is_public": False}).json()["data"]
        _upload(client, kb_id=created["kb_id"])
        resp = client.delete(f"/admin/kb/{created['kb_id']}")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == 3903, "非空库必须 3903"

    def test_empty_kb_deletable_and_grants_cleaned(self, client):
        created = client.post("/admin/kb", json={"name": "制度库", "is_public": False}).json()["data"]
        client.put(f"/admin/kb/{created['kb_id']}/grants", json={"user_ids": ["lisi"]})
        resp = client.delete(f"/admin/kb/{created['kb_id']}")
        assert resp.status_code == 200
        assert resp.json()["data"]["deleted"] is True
        assert all(k["kb_id"] != created["kb_id"] for k in client.get("/admin/kb").json()["data"])


class TestKbUserReverseLookup:
    def test_kbs_of_user_reflects_grants(self, client):
        created = client.post("/admin/kb", json={"name": "制度库", "is_public": False}).json()["data"]
        before = client.get("/admin/users/lisi/kbs").json()["data"]
        assert all(k["kb_id"] != created["kb_id"] for k in before)
        client.put(f"/admin/kb/{created['kb_id']}/grants", json={"user_ids": ["lisi"]})
        after = client.get("/admin/users/lisi/kbs").json()["data"]
        assert any(k["kb_id"] == created["kb_id"] for k in after), "授权后反查必须含该库"
        # 默认库始终在列（公开）
        assert any(k["kb_id"] == "default" for k in after)

    def test_kbs_of_unknown_user_404(self, client):
        resp = client.get("/admin/users/不存在的人/kbs")
        assert resp.status_code == 404


class TestKbCacheVersion:
    def test_cache_key_invalidated_by_grant_change(self, client):
        """KB 版本必须进缓存签名：授权变更后同用户同问题的缓存键必须变化。"""
        pipeline = client.app.state.pipeline.bind_principal("default", "lisi")
        config = client.app.state.config_store.get_active()
        created = client.post("/admin/kb", json={"name": "制度库", "is_public": False}).json()["data"]
        k_before = pipeline._cache_key("同问", "default", config)
        client.put(f"/admin/kb/{created['kb_id']}/grants", json={"user_ids": ["lisi"]})
        k_after = pipeline._cache_key("同问", "default", config)
        assert k_before != k_after, "授权变更后旧缓存键必须失效（否则撤权后仍命中旧答案）"
