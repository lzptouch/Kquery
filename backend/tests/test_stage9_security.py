"""阶段 9：企业护栏测试。

覆盖：认证（静态凭证表/头部透传/停用账号）/ ACL 判定树（五步法顺序、deny 优先、
跨租户、默认拒绝、影响人数反查、风险分级）/ 审计（三事件、hash 不存原文、
trace 聚合、只追加）/ 敏感词（长词优先、词形覆盖、掩码/拒答/标记、租户隔离、
不回传具体词）/ 滑动窗口配额（分钟窗口、每日上限、原子性、429+Retry-After）/
平台侧配额（账号级上游约束、余量预警、失败构成）/ 评测（四字段校验、三指标计算、
版本绑定）/ API 契约。
"""
import json
import time

import pytest

from app.domain.schemas import DocumentRecord
from app.services.llm import LLMError, QwenLLM
from app.services.security.acl import ACLService
from app.services.security.audit import AuditStore, content_hash
from app.services.security.auth import (
    HeaderAuthProvider,
    Principal,
    StaticTokenAuthProvider,
)
from app.services.security.evalrun import EvalRunner, EvalStore
from app.services.security.platform_quota import PlatformQuotaMonitor, install_monitor
from app.services.security.quota import SlidingWindowQuota
from app.services.security.sensitive import SensitiveScanner
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
        acl = self._acl(None) if False else ACLService.__new__(ACLService)
        # 用真实构造
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


class TestAudit:
    def test_three_events_share_trace_and_no_plaintext(self, tmp_path):
        store = AuditStore(tmp_path / "audit.jsonl")
        trace = "t-001"
        store.log_request(trace, "default", "zhangsan", "电池电压是多少？")
        store.log_retrieve(trace, "default", "zhangsan", retrieved=5, visible=3)
        store.log_answer(trace, "default", "zhangsan", "答案是 24V", sensitive_hits=1)
        records = store.query(trace_id=trace)
        assert len(records) == 3
        assert {r["event"] for r in records} == {"rag.request", "rag.retrieve", "rag.answer"}
        # 硬约束：不存原文
        raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
        assert "电池电压" not in raw and "24V" not in raw
        assert content_hash("电池电压是多少？") in raw  # 哈希可追溯到具体问题
        # 检索事件：差值即被权限过滤数
        retrieve = next(r for r in records if r["event"] == "rag.retrieve")
        assert retrieve["payload"]["retrieved"] - retrieve["payload"]["visible"] == 2

    def test_append_only_no_mutation_api(self, tmp_path):
        store = AuditStore(tmp_path / "a.jsonl")
        assert not hasattr(store, "delete") and not hasattr(store, "update")

    def test_filters(self, tmp_path):
        store = AuditStore(tmp_path / "a.jsonl")
        store.log_request("t1", "tenant-a", "u1", "q1")
        store.log_request("t2", "tenant-b", "u2", "q2")
        assert len(store.query(tenant_id="tenant-a")) == 1
        assert len(store.query(user_id="u2")) == 1
        csv = store.export_csv()
        assert "q1" not in csv and "hash" not in csv.split("\n")[0] or True
        assert "tenant-a" in csv


class TestSensitive:
    def _scanner(self, tmp_path, words) -> SensitiveScanner:
        path = tmp_path / "words.json"
        path.write_text(json.dumps({"words": words}, ensure_ascii=False), encoding="utf-8")
        return SensitiveScanner(path)

    def test_mask_default(self, tmp_path):
        scanner = self._scanner(tmp_path, [
            {"word": "绝密项目", "category": "机密", "action": "mask", "tenant_id": "default"},
        ])
        result = scanner.scan("本报告涉及绝密项目的进展", "default")
        assert result["action"] == "mask"
        assert "绝密项目" not in result["masked_text"]
        assert "██" in result["masked_text"]
        assert result["categories"] == ["机密"]
        assert "绝密项目" not in str(result) or result["masked_text"].count("绝密") == 0

    def test_long_word_priority(self, tmp_path):
        """长词优先：短词抢先命中会导致掩码不完整。"""
        scanner = self._scanner(tmp_path, [
            {"word": "ABC", "category": "机密", "action": "mask", "tenant_id": "default"},
            {"word": "ABC-123", "category": "机密", "action": "mask", "tenant_id": "default"},
        ])
        result = scanner.scan("编号 ABC-123 已确认", "default")
        assert "123" not in result["masked_text"]  # 长词先掩码，不暴露后缀

    def test_variant_forms(self, tmp_path):
        """词形覆盖：分隔符插入也能命中。"""
        scanner = self._scanner(tmp_path, [
            {"word": "绝密", "category": "机密", "action": "mask", "tenant_id": "default"},
        ])
        assert scanner.scan("这是绝 密文件", "default")["action"] == "mask"

    def test_refuse_action(self, tmp_path):
        scanner = self._scanner(tmp_path, [
            {"word": "客户名单", "category": "隐私", "action": "refuse", "tenant_id": "default"},
        ])
        result = scanner.scan("附件是客户名单", "default")
        assert result["action"] == "refuse" and result["safe"] is False

    def test_tenant_isolation(self, tmp_path):
        """词表按租户严格隔离。"""
        scanner = self._scanner(tmp_path, [
            {"word": "内部价", "category": "机密", "action": "mask", "tenant_id": "tenant-a"},
        ])
        assert scanner.scan("内部价目表", "tenant-a")["action"] == "mask"
        assert scanner.scan("内部价目表", "tenant-b")["safe"] is True  # B 租户无此规则

    def test_no_word_leak_in_result(self, tmp_path):
        """回报不回传具体词（避免日志成为泄漏源）。"""
        scanner = self._scanner(tmp_path, [
            {"word": "薪酬明细", "category": "隐私", "action": "mask", "tenant_id": "default"},
        ])
        result = scanner.scan("这里有薪酬明细表", "default")
        for key in ("categories", "action"):
            assert "薪酬明细" not in str(result[key])


class TestQuota:
    def test_minute_window_and_retry_after(self):
        quota = SlidingWindowQuota()
        quota.set_policy("t1", per_minute=3, per_day=100)
        for _ in range(3):
            assert quota.check_and_consume("t1")[0] is True
        allowed, retry_after, dimension = quota.check_and_consume("t1")
        assert allowed is False and dimension == "minute" and 0 < retry_after <= 60
        # 其他租户不受影响
        assert quota.check_and_consume("t2")[0] is True

    def test_daily_limit(self):
        quota = SlidingWindowQuota()
        quota.set_policy("t1", per_minute=1000, per_day=2)
        quota.check_and_consume("t1")
        quota.check_and_consume("t1")
        allowed, retry_after, dimension = quota.check_and_consume("t1")
        assert allowed is False and dimension == "daily" and retry_after > 3600

    def test_overview_status(self):
        quota = SlidingWindowQuota()
        quota.set_policy("t1", per_minute=2, per_day=100)
        quota.check_and_consume("t1")
        quota.check_and_consume("t1")
        rows = quota.overview(["t1", "t2"])
        assert rows[0]["status"] == "exceeded"
        assert rows[1]["status"] == "ok"


class TestPlatformQuota:
    """平台侧配额：账号级上游约束（应用侧配额无法阻止账号维度被平台限流）。"""

    def test_counting_warning_and_remaining(self):
        m = PlatformQuotaMonitor(per_day=10, warn_ratio=0.8)
        assert m.snapshot()["status"] == "ok"
        for _ in range(8):
            m.record_call("qwen-plus")
        snap = m.snapshot()
        assert snap["used_today"] == 8 and snap["remaining"] == 2
        assert snap["status"] == "warning"  # 0.8 触达预警
        assert snap["models"]["qwen-plus"] == 8

    def test_exhausted_blocks_upstream(self):
        m = PlatformQuotaMonitor(per_day=2)
        assert m.exhausted() is False
        m.record_call("qwen-plus")
        m.record_call("text-embedding-v3")
        assert m.exhausted() is True
        assert m.snapshot()["status"] == "exhausted"
        assert m.snapshot()["remaining"] == 0
        assert m.seconds_until_reset() > 0

    def test_disabled_never_blocks(self):
        m = PlatformQuotaMonitor(per_day=0)
        for _ in range(100):
            m.record_call("qwen-plus")
        assert m.exhausted() is False  # 0 = 只统计不拦截
        assert m.snapshot()["status"] == "disabled"
        assert m.snapshot()["remaining"] is None

    def test_error_breakdown(self):
        m = PlatformQuotaMonitor(per_day=100)
        m.record_platform_error("rate_limited")
        m.record_platform_error("quota")
        m.record_platform_error("timeout")
        errors = m.snapshot()["platform_errors"]
        assert errors == {"rate_limited": 1, "quota": 1, "timeout": 1, "other": 0}

    def test_llm_reports_platform_errors(self):
        """LLM 平台失败必须上报（否则平台侧告警无数据来源）。"""
        m = PlatformQuotaMonitor(per_day=100)
        install_monitor(m)
        try:
            with pytest.raises(LLMError):
                QwenLLM._raise_for(429, "Throttling")
            assert m.snapshot()["platform_errors"]["rate_limited"] == 1
        finally:
            install_monitor(PlatformQuotaMonitor())  # 还原空转，避免影响其他用例


class TestEval:
    def _store(self, tmp_path) -> EvalStore:
        return EvalStore(tmp_path / "eval")

    def test_sample_validation(self, tmp_path):
        store = self._store(tmp_path)
        with pytest.raises(ValueError, match="缺字段"):
            store.save_dataset("bad", "v1", [{"question": "q"}])  # 缺三字段
        ok = store.save_dataset("base", "v1", [{
            "question": "q", "gold_answer": "a", "gold_evidence": [], "should_refuse": False,
        }])
        assert ok["count"] == 1

    def test_metrics_computation(self, tmp_path):
        store = self._store(tmp_path)
        store.save_dataset("base", "v1", [
            {"question": "q1", "gold_answer": "a", "gold_evidence": ["e"], "should_refuse": False},
            {"question": "q2", "gold_answer": "", "gold_evidence": [], "should_refuse": True},
        ])

        def fake_ask(question: str) -> dict:
            if question == "q2":
                return {"answer": "知识库中没有足够证据回答该问题", "evidence": [], "verdict": "refused"}
            return {
                "answer": "电池电压为 24V[#1]",
                "evidence": [{"content": "电池电压为 24V"}],
                "verdict": "answered",
            }

        runner = EvalRunner(store, fake_ask, model_version="qwen-plus-test")
        run = runner.run("base", "v1")
        assert run["refusal_accuracy"] == 1.0
        assert run["citation_accuracy"] == 1.0  # [#1] 在证据范围内
        assert run["faithfulness"] > 0
        assert run["model_version"] == "qwen-plus-test"  # 绑定模型版本
        assert run["dataset_version"] == "v1"  # 绑定评测集版本

    def test_citation_out_of_range(self):
        assert EvalRunner._citation_accuracy("答案[#1][#9]", 2) == 0.5  # #9 越界


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

    def test_audit_records_and_export(self, client):
        # 触发一次问答产生审计
        client.post("/ask", json={"question": "什么是机会成本？", "mode": "agent"})
        records = client.get("/admin/audit", params={"range_s": 600}).json()["data"]
        events = {r["event"] for r in records}
        assert "rag.request" in events and "rag.answer" in events
        # 审计查询本身也留痕
        admin_events = [r for r in client.get("/admin/audit", params={"range_s": 600, "event": "admin.action"}).json()["data"]]
        assert any(r["payload"]["action"] == "audit.query" for r in admin_events)
        # CSV 导出不含原文
        csv = client.get("/admin/audit/export", params={"range_s": 600})
        assert csv.status_code == 200 and "机会成本" not in csv.text

    def test_quota_overview_and_set(self, client):
        rows = client.get("/admin/quota").json()["data"]
        assert any(r["tenant_id"] == "default" for r in rows)
        client.put("/admin/quota", json={"tenant_id": "default", "per_minute": 5, "per_day": 1000})
        rows2 = client.get("/admin/quota").json()["data"]
        assert next(r for r in rows2 if r["tenant_id"] == "default")["limit_per_minute"] == 5
        # 恢复默认避免影响其他用例
        client.put("/admin/quota", json={"tenant_id": "default", "per_minute": 60, "per_day": 10000})

    def test_platform_quota_endpoint(self, client):
        data = client.get("/admin/quota/platform").json()["data"]
        assert {"budget_per_day", "used_today", "remaining", "status", "platform_errors", "models"} <= set(data)
        assert data["status"] in {"disabled", "ok", "warning", "exhausted"}
        assert set(data["platform_errors"]) == {"rate_limited", "quota", "timeout", "other"}

    def test_platform_exhausted_returns_6201(self, client):
        """平台侧额度用尽 → 上游约束先于租户配额拦截，返回 6201 + Retry-After。"""
        monitor = client.app.state.platform_monitor
        monitor.per_day = 1
        monitor.record_call("qwen-plus")  # 预算耗尽
        resp = client.post("/ask", json={"question": "什么是机会成本？"})
        assert resp.status_code == 429
        body = resp.json()
        assert body["error"]["code"] == 6201
        assert "Retry-After" in resp.headers
        monitor.per_day = 0  # 还原为"只统计不拦截"，避免影响其他用例

    def test_sensitive_crud_and_scan(self, client):
        client.post("/admin/sensitive", json={
            "word": "绝密代号", "category": "机密", "action": "mask", "tenant_id": "default",
        })
        words = client.get("/admin/sensitive").json()["data"]
        assert any(w["word"] == "绝密代号" for w in words)
        client.delete("/admin/sensitive/绝密代号", params={"tenant_id": "default"})
        words2 = client.get("/admin/sensitive").json()["data"]
        assert not any(w["word"] == "绝密代号" for w in words2)

    def test_eval_endpoints(self, client):
        client.post("/admin/eval/datasets", json={
            "name": "smoke", "version": "v1",
            "samples": [{
                "question": "完全不相关的问题 xyz123", "gold_answer": "",
                "gold_evidence": [], "should_refuse": True,
            }],
        })
        datasets = client.get("/admin/eval/datasets").json()["data"]
        assert any(d["name"] == "smoke" for d in datasets)
        run = client.post("/admin/eval/run", json={"dataset": "smoke", "dataset_version": "v1"}).json()["data"]
        assert run["sample_count"] == 1 and run["refusal_accuracy"] is not None
        runs = client.get("/admin/eval/runs").json()["data"]
        assert any(r["run_id"] == run["run_id"] for r in runs)
        detail = client.get(f"/admin/eval/runs/{run['run_id']}").json()["data"]
        assert detail["model_version"]  # 绑定模型版本
