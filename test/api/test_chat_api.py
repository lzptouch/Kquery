import pytest


import uuid


@pytest.mark.api
class TestChatAPI:
    async def test_chat_without_rag(self, client, auth_headers, assert_success) -> None:
        payload = {
            "query": "你好",
            "session_id": f"s-api-{uuid.uuid4().hex[:6]}",
            "collection_name": "",
            "enable_rag": False,
        }
        resp = await client.post("/api/v1/agent/chat", json=payload, headers=auth_headers)
        body = assert_success(resp, expected_status=resp.status_code, required_fields=["success", "session_id", "response"])
        assert body["success"] is True

    async def test_chat_with_rag(self, client, auth_headers, assert_success) -> None:
        # 先创建知识库（使用唯一名称避免冲突）
        kb_name = f"聊天测试-{uuid.uuid4().hex[:6]}"
        kb_resp = await client.post("/api/v1/knowledge-bases", json={"name": kb_name}, headers=auth_headers)
        kb = kb_resp.json()
        payload = {
            "query": "什么是 RAG",
            "session_id": f"s-api-{uuid.uuid4().hex[:6]}",
            "collection_name": kb["collection_name"],
            "enable_rag": True,
        }
        resp = await client.post("/api/v1/agent/chat", json=payload, headers=auth_headers)
        body = assert_success(resp, expected_status=resp.status_code, required_fields=["success", "response"])
        assert body["success"] is True

    async def test_chat_too_many_images(self, client, auth_headers, assert_error) -> None:
        images = [
            {"data_url": f"data:image/png;base64,{'a' * 20}"} for _ in range(4)
        ]
        payload = {"query": "看图片", "session_id": "s-api-3", "images": images}
        resp = await client.post("/api/v1/agent/chat", json=payload, headers=auth_headers)
        assert_error(resp, expected_status=400)

    async def test_chat_invalid_image_format(self, client, auth_headers, assert_error) -> None:
        images = [{"data_url": "data:image/svg+xml;base64,abc"}]
        payload = {"query": "看图片", "session_id": "s-api-4", "images": images}
        resp = await client.post("/api/v1/agent/chat", json=payload, headers=auth_headers)
        assert_error(resp, expected_status=400)
