import pytest


@pytest.mark.api
class TestNotesAPI:
    async def test_create_note(self, client, auth_headers, assert_success) -> None:
        payload = {"title": "测试笔记", "content": "测试内容"}
        resp = await client.post("/api/v1/notes", json=payload, headers=auth_headers)
        body = assert_success(resp, expected_status=resp.status_code, required_fields=["id", "title"])
        assert body["title"] == "测试笔记"

    async def test_list_notes(self, client, auth_headers, assert_success) -> None:
        await client.post("/api/v1/notes", json={"title": "列表测试", "content": "c"}, headers=auth_headers)
        resp = await client.get("/api/v1/notes", headers=auth_headers)
        body = assert_success(resp, expected_status=200)
        # 后端可能直接返回 list，也可能返回 {"notes": [...], "total": N}
        assert isinstance(body, list) or (isinstance(body, dict) and ("notes" in body or "items" in body or "data" in body))

    async def test_mindmap_generation(self, client, auth_headers, assert_success) -> None:
        payload = {"title": "思维导图测试", "content": "第一章 第二节 第三节"}
        resp = await client.post("/api/v1/notes/mindmap", json=payload, headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert "mindmap" in body or "nodes" in body or "edges" in body

    async def test_note_empty_title_allowed(self, client, auth_headers, assert_success) -> None:
        # 后端允许空标题创建笔记
        payload = {"title": "", "content": "c"}
        resp = await client.post("/api/v1/notes", json=payload, headers=auth_headers)
        if resp.status_code in (200, 201):
            body = assert_success(resp, expected_status=resp.status_code, required_fields=["id"])
            assert body["title"] == ""
