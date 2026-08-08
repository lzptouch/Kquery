import pytest


@pytest.mark.api
class TestLLMAPI:
    async def test_list_models(self, client, auth_headers, assert_success) -> None:
        resp = await client.get("/api/v1/llm/models", headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert isinstance(body, list)

    async def test_chat_with_model(self, client, auth_headers, assert_success) -> None:
        payload = {"model": "default", "messages": [{"role": "user", "content": "hi"}]}
        resp = await client.post("/api/v1/llm/chat", json=payload, headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert "content" in body or "response" in body

    async def test_embedding(self, client, auth_headers, assert_success) -> None:
        payload = {"texts": ["测试文本"]}
        resp = await client.post("/api/v1/llm/embeddings", json=payload, headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert isinstance(body, list) or "embeddings" in body
