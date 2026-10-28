"""KB 判定与 ACL 判定在兜底闭包内的组合语义。

要解决的问题：检索链路有两条独立过滤路径——下推（provider，可降级）与
后过滤（`acl_filter_of` 闭包，唯一兜底）。KB 闸门只落在 provider 上时，
provider 一旦降级（清单过大 / 枚举异常），私有库文档会经「tenant 粗过滤 +
ACL 恒放行（acl_public 默认 True）」漏出——这是默认路径上的确定越权。

本文件锁定：闭包内 ACL 与 KB 必须都通过，且 KB 判定在 ACL 之后短路。
"""
from types import SimpleNamespace

from app.core.envelope import ApiError, ErrCode
from app.dependencies import acl_filter_of
from app.domain.schemas import DocumentRecord
from app.services.security.acl import ACLService
from app.services.security.auth import Principal, UserDirectory
from app.services.security.kb import DEFAULT_KB_ID, KBService


class _Knowledge:
    """最小文档目录替身：只实现闭包用到的 get/list_files。"""

    def __init__(self, docs: list[DocumentRecord]):
        self._docs = {d.doc_id: d for d in docs}

    def get(self, doc_id: str) -> DocumentRecord:
        doc = self._docs.get(doc_id)
        if doc is None:
            raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, "不存在", http_status=404)
        return doc

    def list_files(self, tenant_id: str | None = None) -> list[DocumentRecord]:
        return list(self._docs.values())


def _make_check(tmp_path, docs, kb_enabled=True, acl_enabled=True):
    """用真实服务 + 替身目录构造闭包（不经 HTTP 层，聚焦组合语义）。"""
    directory = UserDirectory(tmp_path / "users.json")
    acl = ACLService(tmp_path / "acl.json", directory)
    kb = KBService(tmp_path / "kbs.json", tmp_path / "grants.json", directory)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(acl_enabled=acl_enabled, kb_enabled=kb_enabled),
                acl_service=acl,
                knowledge=_Knowledge(docs),
                kb_service=kb,
            )
        )
    )
    return acl, kb, request


def _doc(doc_id: str, kb_id: str = DEFAULT_KB_ID) -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id, file_id="f", filename=f"{doc_id}.md", tenant_id="default",
        source_path="/x", content_hash="h", knowledge_base_id=kb_id,
    )


class TestAclKbClosureComposition:
    def test_acl_pass_and_kb_deny_rejects(self, tmp_path):
        """文档 ACL 放行（默认公开）但 KB 未授权 → 拒绝。

        这是核心用例：acl_public 默认 True 意味着 ACL 对多数文档恒放行，
        KB 是这类文档的唯一闸门——闭包若不查 KB 就是确定越权。
        """
        _, kb, request = _make_check(tmp_path, [_doc("a")])
        private = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        request.app.state.knowledge._docs["b"] = _doc("b", private.kb_id)
        check = acl_filter_of(request, Principal(user_id="lisi", tenant_id="default"))
        assert check("a") is True, "默认库（公开）应放行"
        assert check("b") is False, "私有库未授权必须拒绝（ACL 放行不是终点）"

    def test_acl_deny_short_circuits_before_kb(self, tmp_path):
        """ACL 先拒的文档不再查 KB（短路，与判定树顺序一致）。"""
        acl, kb, request = _make_check(tmp_path, [_doc("a")])
        acl.set_acl("a", [], ["user:lisi"], False, updated_by="admin")
        private = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        kb.set_grants(private.kb_id, ["lisi"], granted_by="admin")
        request.app.state.knowledge._docs["a"].knowledge_base_id = private.kb_id
        check = acl_filter_of(request, Principal(user_id="lisi", tenant_id="default"))
        assert check("a") is False, "deny 优先：KB 授权不能救回被 ACL 拉黑者"

    def test_kb_disabled_falls_back_to_acl_only(self, tmp_path):
        """kb_enabled=false 时 KB 判定整体跳过，行为逐位退回现状。"""
        _, kb, request = _make_check(tmp_path, [_doc("a")], kb_enabled=False)
        private = kb.create("私有库", "", False, tenant_id="default", created_by="admin")
        request.app.state.knowledge._docs["b"] = _doc("b", private.kb_id)
        check = acl_filter_of(request, Principal(user_id="lisi", tenant_id="default"))
        assert check("b") is True, "kb_enabled=false 时必须退回纯 ACL 行为（回滚语义）"

    def test_acl_disabled_returns_none_and_kb_is_also_off(self, tmp_path):
        """acl_enabled=false 时整个闭包不建——KB 闸门随之失效（有意耦合：
        「不设权限」与「只做 KB 权限」在语义上矛盾，不允许后者单独存在）。"""
        _, kb, request = _make_check(tmp_path, [_doc("a")], acl_enabled=False)
        assert acl_filter_of(request, Principal(user_id="lisi", tenant_id="default")) is None

    def test_builtin_and_quickask_prefixes_exempt_from_kb(self, tmp_path):
        """系统内置块（__）与快问临时文档（quick_）不属任何 KB，必须豁免——
        否则图谱社区摘要与快问会被私有库判定误拦。"""
        _, _, request = _make_check(tmp_path, [_doc("a")])
        check = acl_filter_of(request, Principal(user_id="lisi", tenant_id="default"))
        assert check("__community_x") is True, "内置块不查 KB"
        assert check("quick_abc123") is True, "快问文档不受 KB 约束（D-6）"

    def test_unknown_doc_rejects(self, tmp_path):
        """无主文档不可见（默认拒绝原则：闭包查不到记录时不放行）。"""
        _, _, request = _make_check(tmp_path, [])
        check = acl_filter_of(request, Principal(user_id="lisi", tenant_id="default"))
        assert check("ghost-doc") is False

    def test_memo_caches_combined_decision(self, tmp_path):
        """请求级记忆化：同一文档 ACL+KB 组合判定只算一次。"""
        acl, _, request = _make_check(tmp_path, [_doc("a")])
        check = acl_filter_of(request, Principal(user_id="lisi", tenant_id="default"))
        assert check("a") is True
        calls = {"n": 0}
        original = acl.check

        def counting(p, d):
            calls["n"] += 1
            return original(p, d)

        acl.check = counting
        check("a")
        assert calls["n"] == 0, "memo 命中不应重复判定"
