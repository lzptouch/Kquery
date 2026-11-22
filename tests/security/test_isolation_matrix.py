"""数据隔离回归（原「越权矩阵」，`X-30`）。

## 为什么不是"角色越权"

认证与授权已整体下线，`Principal` 退化为固定值
（`tenant_id="default"` / `user_id="local"`，见 `app/core/identity.py`）。
因此**没有第二个角色可以伪造**，"越权矩阵"只剩两条真实可攻击的路径：

1. **自报身份**：请求头 `X-Tenant-Id` / `X-User-Id` / `X-Roles`、请求体 `user_id`
   都不能改变主体——否则数据隔离维度形同虚设；
2. **按 id 直取**：知道别人的 `session_id` / `doc_id` / `run_id` / `trace_id`
   就能读（甚至删）——这是"猜 id"型越权。

## 断言的统一口径

- 归属不符与**不存在**返回同一个 404 码（不用 403）：403 会告诉攻击者
  "这个 id 存在，只是不属于你"，等于存在性探测预言机；
- 404 响应**不得回显**外租户的资源名/标题（只回显调用方自己传进来的 id 也算边界，
  所以断言只针对标题、文件名这类调用方本来不知道的信息）；
- 越权尝试**不得产生副作用**：改名/删除必须原样失败。

## 已覆盖的隔离面

会话、知识库文档、快问临时文档、HITL run、成本账单 trace、反馈工单、
查询分析记录。图谱实体/图像资产由 `tests/integration/test_batch_b_security.py`
覆盖（同一纪律，避免重复造同一批夹具）。
"""
from __future__ import annotations

from pathlib import Path

from app.core.envelope import ErrCode
from app.core.identity import Principal
from app.services.agent.cost import get_ledger
from app.services.query.schemas import QueryAnalysis

#: 用于自报身份的请求头。全部应当在网关层被忽略（现在连网关都没有，直接忽略）。
IMPERSONATION_HEADERS = {
    "X-Tenant-Id": "tenant-a",
    "X-User-Id": "mallory",
    "X-Roles": "admin",
}

#: 「另一个租户的另一个用户」。认证下线后无法通过 HTTP 产生，只在服务层造夹具。
FOREIGN = Principal(tenant_id="tenant-a", user_id="mallory")


def _code(resp) -> int | None:
    return (resp.json().get("error") or {}).get("code")


class TestIdentityCannotBeSelfReported:
    def test_upload_headers_do_not_change_document_owner(self, client):
        """上传时伪造租户头 → 文档仍归 `default`，且不出现在 `tenant-a` 的列表里。"""
        resp = client.post(
            "/knowledge/upload",
            headers=IMPERSONATION_HEADERS,
            files={"file": ("隔离测试.md", "# 隔离测试\n\n归属必须是 default。".encode(), "text/markdown")},
        )
        assert resp.status_code == 200, resp.text
        doc_id = resp.json()["data"]["doc_id"]

        service = client.app.state.knowledge
        assert service.get(doc_id).tenant_id == "default"
        assert service.list_files(tenant_id="tenant-a") == []
        listed = {f["doc_id"] for f in client.get("/knowledge/files").json()["data"]}
        assert doc_id in listed

    def test_session_headers_do_not_change_session_owner(self, client):
        """建档时伪造租户/用户/角色头 → session_id 前缀仍是 `default:local:`。"""
        resp = client.post(
            "/sessions", json={"title": "伪造归属"}, headers=IMPERSONATION_HEADERS
        )
        assert resp.status_code == 200, resp.text
        session_id = resp.json()["data"]["session_id"]
        assert session_id.startswith("default:local:")

        store = client.app.state.session_store
        assert store.get(session_id, "default", "local")["title"] == "伪造归属"
        # 用伪造的身份去读，必须当作不存在
        try:
            store.get(session_id, FOREIGN.tenant_id, FOREIGN.user_id)
        except Exception as exc:  # ApiError：会话不存在
            assert int(getattr(exc, "code", 0)) == ErrCode.SESSION_NOT_FOUND
        else:  # pragma: no cover - 走到这里说明归属校验失效
            raise AssertionError("伪造身份读到了不属于自己的会话")

    def test_feedback_headers_do_not_change_ticket_owner(self, client):
        """反馈工单是在用户数据，同样按主体落库。"""
        resp = client.post(
            "/feedback/refusal",
            headers=IMPERSONATION_HEADERS,
            json={
                "feedback_type": "missing_doc",
                "refusal_reason": "no_content",
                "user_note": "伪造租户头",
                "trace_id": "trace-isolation-0001",
            },
        )
        assert resp.status_code == 200, resp.text
        ticket_id = resp.json()["data"]["ticket_id"]

        ticket = client.get(f"/feedback/{ticket_id}").json()["data"]
        assert ticket["tenant_id"] == "default"
        assert ticket["user_id"] == "local"


class TestForeignSession:
    def test_list_get_rename_delete_are_all_denied(self, client):
        store = client.app.state.session_store
        session_id = store.create(FOREIGN.tenant_id, FOREIGN.user_id, "外部会话")["session_id"]

        listed = {s["session_id"] for s in client.get("/sessions").json()["data"]}
        assert session_id not in listed

        matrix = [
            client.get(f"/sessions/{session_id}"),
            client.patch(f"/sessions/{session_id}", json={"title": "被改名了"}),
            client.delete(f"/sessions/{session_id}"),
        ]
        for resp in matrix:
            assert resp.status_code == 404, resp.text
            assert _code(resp) == ErrCode.SESSION_NOT_FOUND
            # 不泄漏外租户标题
            assert "外部会话" not in resp.text

        # 越权尝试没有副作用：会话与标题都还在
        assert store.get(session_id, FOREIGN.tenant_id, FOREIGN.user_id)["title"] == "外部会话"


class TestForeignDocument:
    def test_list_preview_delete_rebuild_are_all_denied(self, client):
        service = client.app.state.knowledge
        record = service.create_record(
            "外部机密.md", "# 外部机密\n\n租户 A 的内部资料。".encode(), tenant_id=FOREIGN.tenant_id
        )
        doc_id = record.doc_id

        listed = {f["doc_id"] for f in client.get("/knowledge/files").json()["data"]}
        assert doc_id not in listed

        matrix = [
            client.get(f"/knowledge/preview/{doc_id}"),
            client.get(f"/knowledge/preview/{doc_id}/chunk/c-0"),
            client.post(f"/knowledge/rebuild/{doc_id}"),
            client.post(f"/knowledge/{doc_id}/offline"),
            client.post(f"/knowledge/{doc_id}/reparse", json={}),
            client.delete(f"/knowledge/files/{doc_id}"),
        ]
        for resp in matrix:
            assert resp.status_code == 404, resp.text
            assert _code(resp) == ErrCode.DOCUMENT_NOT_FOUND
            # 只回显调用方自己传进来的 doc_id（doc_id 由文件名 stem 派生），
            # 但不得回显带扩展名的真实文件名——那是调用方本来不知道的信息。
            assert "外部机密.md" not in resp.text

        # 删除必须原样失败：记录仍在，且仍属于 tenant-a
        assert service.get(doc_id).tenant_id == FOREIGN.tenant_id


class TestForeignRunAccess:
    def test_cancel_is_denied(self, client):
        """run_id 前缀即归属凭证；跨主体一律 404/4301。"""
        run_id = f"{FOREIGN.tenant_id}:{FOREIGN.user_id}:1:0"
        resp = client.post(f"/ask/runs/{run_id}/cancel")
        assert resp.status_code == 404, resp.text
        assert _code(resp) == ErrCode.SESSION_NOT_FOUND


class TestForeignCostAndAnalysis:
    def test_cost_of_foreign_trace_is_not_visible(self, client):
        """成本账单含模型与金额：跨租户读同一 trace 必须看不到。"""
        trace_id = "trace-foreign-cost-0001"
        get_ledger().record(
            trace_id, FOREIGN.tenant_id, "answer", "mock-model", tokens=1234,
            cost_yuan=0.05, priced=True,
        )
        data = client.get(f"/ask/{trace_id}/cost").json()["data"]
        assert data["found"] is False
        assert data["total_tokens"] == 0
        assert data["total_yuan"] == 0.0

    def test_query_analysis_records_are_tenant_scoped(self, client):
        """分析记录含用户原话：列表/详情/模式统计都只能看到自己租户的。"""
        store = client.app.state.analysis_store
        store.save(
            QueryAnalysis(
                analysis_id="an-foreign-0001",
                raw_query="租户 A 的机密提问",
                tenant_id=FOREIGN.tenant_id,
                intent="chitchat",
            )
        )

        assert client.get("/knowledge/query-analysis").json()["data"] == []
        assert client.get("/knowledge/query-patterns").json()["data"] == []

        detail = client.get("/knowledge/query-analysis/an-foreign-0001")
        assert detail.status_code == 404
        assert _code(detail) == ErrCode.DOCUMENT_NOT_FOUND
        assert "机密提问" not in detail.text

        # 伪造租户头也不能读到
        spoofed = client.get(
            "/knowledge/query-analysis/an-foreign-0001", headers=IMPERSONATION_HEADERS
        )
        assert spoofed.status_code == 404


class TestFeedbackTicketScoping:
    def test_foreign_ticket_is_404_and_does_not_echo_notes(self, client):
        store = client.app.state.feedback_store
        ticket = store.create(
            "other", "", "外部用户的抱怨", "trace-foreign-feedback",
            tenant_id=FOREIGN.tenant_id, user_id=FOREIGN.user_id,
        )
        resp = client.get(f"/feedback/{ticket.ticket_id}")
        assert resp.status_code == 404
        assert _code(resp) == ErrCode.DOCUMENT_NOT_FOUND
        assert "外部用户的抱怨" not in resp.text
