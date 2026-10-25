"""F4 快问端到端：上传即问、范围隔离、删除后拒绝、非 admin 可用。"""
import json

QUICK_MD = """# 快问专用文档

## 独占内容

快问文档里的唯一密钥是 ZEBRAFISH-7749，用于验证检索范围隔离。

## 其它说明

这是一份只存在于临时文档中的内容，不应出现在知识库文件列表里。
"""

USER_HEADERS = {"X-User-Id": "alice", "X-Tenant-Id": "default"}


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        event = ""
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if event and data:
            try:
                events.append((event, json.loads(data)))
            except json.JSONDecodeError:
                pass
    return events


class TestQuickAsk:
    def test_upload_parse_ask_evidence(self, client):
        resp = client.post(
            "/quickask/upload",
            files={"file": ("临时.md", QUICK_MD.encode("utf-8"), "text/markdown")},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        doc_id = data["doc_id"]
        assert doc_id.startswith("quick_")
        assert data["chunks"] > 0

        with client.stream(
            "POST", "/quickask/ask",
            json={"question": "快问文档里的唯一密钥是什么？", "doc_id": doc_id},
        ) as resp:
            assert resp.status_code == 200
            body = "".join(resp.iter_text())
        events = _sse_events(body)
        names = [e for e, _ in events]
        assert "done" in names, names
        evidence = [p for e, p in events if e == "evidence"]
        assert evidence, f"应有证据事件：{names}"
        assert all(ev["doc_id"] == doc_id for ev in evidence), "证据必须全部来自该临时文档"
        assert "ZEBRAFISH-7749" in body

    def test_not_in_knowledge_list(self, client):
        client.post(
            "/quickask/upload",
            files={"file": ("临时.md", QUICK_MD.encode("utf-8"), "text/markdown")},
        )
        files = client.get("/knowledge/files").json()["data"]
        assert all(f["filename"] != "临时.md" for f in files), "快问文档不得进知识库列表"

    def test_ask_scoped_to_document(self, client):
        """知识库里有同名内容时，快问答案的证据仍只能来自快问文档。"""
        client.post(
            "/knowledge/upload",
            files={"file": ("同名.md", "# 同名\n\n知识库版本的内容是 KOROBO-1234。\n".encode("utf-8"), "text/markdown")},
        )
        resp = client.post(
            "/quickask/upload",
            files={"file": ("同名.md", QUICK_MD.encode("utf-8"), "text/markdown")},
        )
        doc_id = resp.json()["data"]["doc_id"]

        with client.stream(
            "POST", "/quickask/ask",
            json={"question": "唯一密钥是什么？", "doc_id": doc_id},
        ) as resp:
            body = "".join(resp.iter_text())
        evidence = [p for e, p in _sse_events(body) if e == "evidence"]
        assert evidence
        assert all(ev["doc_id"] == doc_id for ev in evidence)
        assert "KOROBO-1234" not in body, "快问答案不得混入知识库其它文档的内容"

    def test_ask_after_delete_refused(self, client):
        resp = client.post(
            "/quickask/upload",
            files={"file": ("临时.md", QUICK_MD.encode("utf-8"), "text/markdown")},
        )
        doc_id = resp.json()["data"]["doc_id"]
        assert client.delete(f"/quickask/{doc_id}").status_code == 200

        # 删除后再问：在流开始**之前**就拒绝（结构化 JSON 错误，而不是先吐 token 再报错）
        resp = client.post(
            "/quickask/ask", json={"question": "密钥是什么？", "doc_id": doc_id}
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == 3004, body

    def test_non_admin_can_use_quickask(self, client):
        resp = client.post(
            "/quickask/upload",
            files={"file": ("临时.md", QUICK_MD.encode("utf-8"), "text/markdown")},
            headers=USER_HEADERS,
        )
        assert resp.status_code == 200, resp.text
        doc_id = resp.json()["data"]["doc_id"]
        assert client.get(f"/quickask/{doc_id}", headers=USER_HEADERS).status_code == 200

    def test_other_user_cannot_read(self, client):
        resp = client.post(
            "/quickask/upload",
            files={"file": ("临时.md", QUICK_MD.encode("utf-8"), "text/markdown")},
            headers=USER_HEADERS,
        )
        doc_id = resp.json()["data"]["doc_id"]
        other = {"X-User-Id": "mallory", "X-Tenant-Id": "default"}
        assert client.get(f"/quickask/{doc_id}", headers=other).status_code == 404

    def test_ask_endpoint_accepts_doc_id(self, client):
        """普通 /ask 也能带 doc_id（前端可复用同一条 SSE 通道）。"""
        resp = client.post(
            "/quickask/upload",
            files={"file": ("临时.md", QUICK_MD.encode("utf-8"), "text/markdown")},
        )
        doc_id = resp.json()["data"]["doc_id"]
        with client.stream(
            "POST", "/ask/stream",
            json={"question": "唯一密钥是什么？", "doc_id": doc_id},
        ) as resp:
            body = "".join(resp.iter_text())
        evidence = [p for e, p in _sse_events(body) if e == "evidence"]
        assert evidence
        assert all(ev["doc_id"] == doc_id for ev in evidence)

    def test_ask_with_foreign_doc_id_rejected(self, client):
        resp = client.post(
            "/ask", json={"question": "任意问题", "doc_id": "quick_notmine"}
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == 3004
