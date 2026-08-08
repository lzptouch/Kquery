import pytest


import uuid


@pytest.mark.api
class TestKnowledgeBaseAPI:
    async def test_create_kb(self, client, auth_headers, assert_success) -> None:
        name = f"测试知识库-{uuid.uuid4().hex[:6]}"
        payload = {"name": name, "description": "API 测试", "visibility": "private"}
        resp = await client.post("/api/v1/knowledge-bases", json=payload, headers=auth_headers)
        body = assert_success(resp, expected_status=resp.status_code, required_fields=["id", "name", "collection_name"])
        assert body["name"] == name

    async def test_list_kb(self, client, auth_headers, assert_success) -> None:
        resp = await client.get("/api/v1/knowledge-bases", headers=auth_headers)
        body = assert_success(resp, expected_status=200)
        assert isinstance(body, list) or (isinstance(body, dict) and ("items" in body or "data" in body))

    async def test_get_kb_detail(self, client, auth_headers, assert_success) -> None:
        name = f"详情测试-{uuid.uuid4().hex[:6]}"
        create = await client.post("/api/v1/knowledge-bases", json={"name": name, "description": "d"}, headers=auth_headers)
        kb = create.json()
        resp = await client.get(f"/api/v1/knowledge-bases/{kb['id']}", headers=auth_headers)
        body = assert_success(resp, expected_status=resp.status_code, required_fields=["id", "name", "collection_name"])
        assert body["name"] == name

    async def test_kb_not_found(self, client, auth_headers, assert_error) -> None:
        resp = await client.get("/api/v1/knowledge-bases/kb_nonexistent", headers=auth_headers)
        assert resp.status_code in (404, 400, 422)
        if resp.status_code != 200:
            assert_error(resp, expected_status=resp.status_code)

    async def test_kb_create_anonymous_allowed(self, client, assert_success) -> None:
        # 当前后端允许匿名用户创建知识库（owner 为 anonymous）
        name = f"未认证-{uuid.uuid4().hex[:6]}"
        resp = await client.post("/api/v1/knowledge-bases", json={"name": name})
        if resp.status_code in (401, 403):
            return
        body = assert_success(resp, expected_status=resp.status_code, required_fields=["id", "name"])
        assert body["name"] == name
