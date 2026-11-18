"""F10 快问的会话维度：临时文档绑定会话、跨会话拒绝、会话内文档列表。"""
import io

SAMPLE = """# 会话样例

只有在这个会话里才能问到的内容：内部代号是 XJ-42。
"""


def _upload(client, session_id: str | None = None):
    files = {"file": ("会话样例.md", io.BytesIO(SAMPLE.encode("utf-8")), "text/markdown")}
    data = {"session_id": session_id} if session_id is not None else None
    return client.post("/quickask/upload", files=files, data=data)


class TestQuickAskSession:
    def test_upload_binds_session(self, client):
        resp = _upload(client, "sess-A")
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["session_id"] == "sess-A"

    def test_upload_without_session_is_unbound(self, client):
        resp = _upload(client)
        assert resp.status_code == 200
        assert resp.json()["data"]["session_id"] == ""

    def test_ask_in_same_session_allowed(self, client):
        doc_id = _upload(client, "sess-A").json()["data"]["doc_id"]
        resp = client.post(
            "/quickask/ask",
            json={"question": "内部代号是什么？", "doc_id": doc_id, "session_id": "sess-A"},
        )
        assert resp.status_code == 200, resp.text

    def test_ask_in_other_session_rejected(self, client):
        doc_id = _upload(client, "sess-A").json()["data"]["doc_id"]
        resp = client.post(
            "/quickask/ask",
            json={"question": "内部代号是什么？", "doc_id": doc_id, "session_id": "sess-B"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["error"]["code"] == 3004
        # 不泄露"文档存在但不属于该会话"这一事实
        assert "不存在" in body["error"]["message"] or "会话" in body["error"]["message"]

    def test_ask_endpoint_enforces_session(self, client):
        """普通 /ask 通道带 doc_id 时也要校验会话维度。"""
        doc_id = _upload(client, "sess-A").json()["data"]["doc_id"]
        resp = client.post(
            "/ask",
            json={"question": "内部代号是什么？", "doc_id": doc_id, "session_id": "sess-B"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == 3004

    def test_ask_stream_enforces_session(self, client):
        doc_id = _upload(client, "sess-A").json()["data"]["doc_id"]
        resp = client.post(
            "/ask/stream",
            json={"question": "内部代号是什么？", "doc_id": doc_id, "session_id": "sess-B"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == 3004

    def test_unbound_doc_usable_anywhere(self, client):
        """未绑定会话的文档（上传即问）不受会话限制。"""
        doc_id = _upload(client).json()["data"]["doc_id"]
        resp = client.post(
            "/quickask/ask",
            json={"question": "内部代号是什么？", "doc_id": doc_id, "session_id": "sess-Z"},
        )
        assert resp.status_code == 200, resp.text

    def test_list_session_documents(self, client):
        doc_a = _upload(client, "sess-A").json()["data"]["doc_id"]
        _upload(client, "sess-B")

        resp = client.get("/quickask/documents", params={"session_id": "sess-A"})
        assert resp.status_code == 200
        items = resp.json()["data"]
        assert [d["doc_id"] for d in items] == [doc_a]
        assert items[0]["filename"] == "会话样例.md"
        assert items[0]["chunks"] >= 1

    def test_list_other_session_is_empty(self, client):
        _upload(client, "sess-A")
        resp = client.get("/quickask/documents", params={"session_id": "sess-B"})
        assert resp.status_code == 200
        assert resp.json()["data"] == []

    def test_status_reports_session(self, client):
        doc_id = _upload(client, "sess-A").json()["data"]["doc_id"]
        resp = client.get(f"/quickask/{doc_id}")
        assert resp.status_code == 200
        assert resp.json()["data"]["session_id"] == "sess-A"
