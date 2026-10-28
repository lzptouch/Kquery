"""知识库（KB）实体与授权：判定树 / CRUD / 授权影响反查 / 持久化。

要解决的问题：KB 授权是检索链路的一道独立闸门（与文档 ACL 叠加），
判定树的任何一步错位都是越权或误拦。本文件锁定五步判定树的顺序语义。
"""
import json

import pytest

from app.core.envelope import ApiError
from app.domain.schemas import DocumentRecord
from app.services.security.auth import Principal, UserDirectory
from app.services.security.kb import DEFAULT_KB_ID, KBService


def _service(tmp_path):
    return KBService(tmp_path / "kbs.json", tmp_path / "grants.json",
                     UserDirectory(tmp_path / "users.json"))


def _doc(doc_id: str, kb_id: str, tenant_id: str = "default") -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id, file_id="f1", filename=f"{doc_id}.md", tenant_id=tenant_id,
        source_path="/x", content_hash="h", knowledge_base_id=kb_id,
    )


class TestKbDecisionTree:
    """判定树顺序不可调换：租户闸门 → 存在性 → 授权名单 → 公开 → 默认拒绝。"""

    def test_default_kb_created_on_first_boot(self, tmp_path):
        kb = _service(tmp_path)
        default = kb.get(DEFAULT_KB_ID)
        assert default is not None, "默认知识库必须在首次启动时创建"
        assert default.is_public is True, "默认 KB 全员可见（承接存量文档，升级不回归）"

    def test_tenant_mismatch_rejects(self, tmp_path):
        kb = _service(tmp_path)
        other = kb.create("t2库", "", True, tenant_id="tenant-b", created_by="admin")
        principal = Principal(user_id="lisi", tenant_id="default")
        assert kb.check(principal, other.kb_id) is False, "跨租户一律不可见（即便库公开）"

    def test_unknown_kb_rejects(self, tmp_path):
        kb = _service(tmp_path)
        principal = Principal(user_id="lisi", tenant_id="default")
        assert kb.check(principal, "不存在") is False, "不存在的 KB 应拒绝（防探测，不抛异常）"

    def test_granted_user_passes(self, tmp_path):
        kb = _service(tmp_path)
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        kb.set_grants(record.kb_id, ["lisi"], granted_by="admin")
        assert kb.check(Principal(user_id="lisi", tenant_id="default"), record.kb_id) is True

    def test_public_kb_passes_without_grant(self, tmp_path):
        kb = _service(tmp_path)
        record = kb.create("公开库", "", True, tenant_id="default", created_by="admin")
        assert kb.check(Principal(user_id="lisi", tenant_id="default"), record.kb_id) is True

    def test_private_ungranted_rejects(self, tmp_path):
        kb = _service(tmp_path)
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        assert kb.check(Principal(user_id="lisi", tenant_id="default"), record.kb_id) is False, (
            "私有库未授权应默认拒绝"
        )

    def test_check_doc_normalizes_empty_kb_to_default(self, tmp_path):
        """存量文档 knowledge_base_id 为空串时归一化为默认 KB（与加载回填同语义）。"""
        kb = _service(tmp_path)
        doc = _doc("d1", "")
        assert kb.check_doc(Principal(user_id="lisi", tenant_id="default"), doc) is True, (
            "空 kb_id 必须归一化到默认 KB（公开），否则升级后存量文档全员失访"
        )


class TestKbCrud:
    def test_duplicate_name_rejected(self, tmp_path):
        kb = _service(tmp_path)
        kb.create("制度库", "", False, tenant_id="default", created_by="admin")
        with pytest.raises(ApiError) as exc:
            kb.create("制度库", "", False, tenant_id="default", created_by="admin")
        assert exc.value.code.name == "KB_DUPLICATED"

    def test_default_kb_cannot_be_deleted(self, tmp_path):
        kb = _service(tmp_path)
        with pytest.raises(ApiError):
            kb.delete(DEFAULT_KB_ID, doc_count=0)

    def test_non_empty_kb_cannot_be_deleted(self, tmp_path):
        kb = _service(tmp_path)
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        with pytest.raises(ApiError) as exc:
            kb.delete(record.kb_id, doc_count=3)
        assert exc.value.code.name == "KB_NOT_EMPTY", "非空库必须拒绝删除（先移出文档）"

    def test_delete_removes_grants(self, tmp_path):
        kb = _service(tmp_path)
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        kb.set_grants(record.kb_id, ["lisi"], granted_by="admin")
        kb.delete(record.kb_id, doc_count=0)
        assert kb.get(record.kb_id) is None
        assert kb.grants_of(record.kb_id) == [], "删除 KB 必须联动清理授权关系"


class TestKbGrants:
    def test_grant_invalid_user_rejected(self, tmp_path):
        kb = _service(tmp_path)
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        with pytest.raises(ApiError) as exc:
            kb.set_grants(record.kb_id, ["不存在的用户"], granted_by="admin")
        assert exc.value.code.name == "KB_GRANT_INVALID"

    def test_impact_lookup_uses_real_directory(self, tmp_path):
        """影响人数必须后端反查真实用户目录（管理员不该凭用户名猜影响面）。"""
        kb = _service(tmp_path)
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        result = kb.set_grants(record.kb_id, ["lisi", "zhangsan"], granted_by="admin")
        assert result["impact"]["added_count"] == 2
        assert result["impact"]["total_after"] == 2
        # 缩权
        result2 = kb.set_grants(record.kb_id, ["lisi"], granted_by="admin")
        assert result2["impact"]["removed_count"] == 1
        assert result2["impact"]["removed_users"] == ["zhangsan"]
        assert result2["risk"] == "low"

    def test_risk_high_when_expanding(self, tmp_path):
        kb = _service(tmp_path)
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        assert kb.set_grants(record.kb_id, ["lisi"], granted_by="admin")["risk"] == "high"

    def test_grants_of_reflects_enabled_state(self, tmp_path):
        """授权名单必须反查启停状态——对着已停用账号做授权决策是错的。"""
        directory = UserDirectory(tmp_path / "users.json")
        directory.upsert("lisi", {"enabled": False})
        kb = KBService(tmp_path / "kbs.json", tmp_path / "grants.json", directory)
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        # 停用用户不能被授权（_grantable 校验）
        with pytest.raises(ApiError):
            kb.set_grants(record.kb_id, ["lisi"], granted_by="admin")

    def test_version_increments_on_writes(self, tmp_path):
        """全局版本是缓存键维度：任何写操作递增，否则撤权后旧缓存仍命中。"""
        kb = _service(tmp_path)
        v0 = kb.version
        record = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        assert kb.version > v0
        v1 = kb.version
        kb.set_grants(record.kb_id, ["lisi"], granted_by="admin")
        assert kb.version > v1, "授权变更必须递增版本"


class TestKbPersistence:
    def test_roundtrip_survives_reload(self, tmp_path):
        """权限数据是安全边界，重启后必须逐位恢复。"""
        kb = _service(tmp_path)
        record = kb.create("私有库", "描述", False, tenant_id="default", created_by="admin")
        kb.set_grants(record.kb_id, ["lisi"], granted_by="admin")

        kb2 = KBService(tmp_path / "kbs.json", tmp_path / "grants.json",
                        UserDirectory(tmp_path / "users.json"))
        reloaded = kb2.get(record.kb_id)
        assert reloaded is not None and reloaded.name == "私有库"
        assert kb2.check(Principal(user_id="lisi", tenant_id="default"), record.kb_id) is True

    def test_corrupted_store_refuses_to_start(self, tmp_path):
        """权限数据损坏比"没有权限数据"严重：拒绝以空 KB 对外服务。"""
        (tmp_path / "kbs.json").write_text("{ 损坏", encoding="utf-8")
        with pytest.raises(Exception):
            KBService(tmp_path / "kbs.json", tmp_path / "grants.json",
                      UserDirectory(tmp_path / "users.json"))

    def test_atomic_write_leaves_no_partial_json(self, tmp_path):
        kb = _service(tmp_path)
        kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        payload = json.loads((tmp_path / "kbs.json").read_text(encoding="utf-8"))
        assert isinstance(payload, list) and len(payload) == 2  # default + 新建


class TestKbVisibleDocIds:
    def test_visible_doc_ids_expands_grants(self, tmp_path):
        kb = _service(tmp_path)
        private = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        docs = [_doc("a", DEFAULT_KB_ID), _doc("b", private.kb_id)]
        principal = Principal(user_id="lisi", tenant_id="default")
        # 未授权：只有默认库
        assert kb.visible_doc_ids(principal, docs) == {"a"}
        # 授权后：两个库都可见
        kb.set_grants(private.kb_id, ["lisi"], granted_by="admin")
        assert kb.visible_doc_ids(principal, docs) == {"a", "b"}

    def test_kbs_of_user_reverse_lookup(self, tmp_path):
        kb = _service(tmp_path)
        private = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        kb.set_grants(private.kb_id, ["lisi"], granted_by="admin")
        ids = {k.kb_id for k in kb.kbs_of_user("lisi", "default")}
        assert ids == {DEFAULT_KB_ID, private.kb_id}, "反查必须含默认库 + 已授权私有库"
        ids_wangwu = {k.kb_id for k in kb.kbs_of_user("wangwu", "tenant-b")}
        # 默认 KB 是各租户各自的公共桶（不租户过滤，文档级闸门兜底）
        assert DEFAULT_KB_ID in ids_wangwu, "默认库对任意租户可见"
        assert private.kb_id not in ids_wangwu, "default 租户的私有库对 tenant-b 不可见"
