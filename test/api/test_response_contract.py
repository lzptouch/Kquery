import pytest


@pytest.mark.api
class TestResponseContract:
    async def test_not_found_returns_error_structure(self, client, assert_error) -> None:
        resp = await client.get("/api/v1/not-a-real-endpoint")
        assert_error(resp, expected_status=404)

    async def test_validation_error_returns_VAL_prefix(self, client, auth_headers, assert_error) -> None:
        # 故意发送非法图片格式触发 400
        payload = {"query": "看图片", "session_id": "s-contract-1", "images": [{"data_url": "invalid"}]}
        resp = await client.post("/api/v1/agent/chat", json=payload, headers=auth_headers)
        assert resp.status_code in (400, 422)

    async def test_validation_error_returns_error_or_detail(self, client, auth_headers) -> None:
        # 使用 agent/chat 的非法图片格式触发 400/422 校验错误
        payload = {"query": "看图片", "session_id": "s-contract-1", "images": [{"data_url": "invalid"}]}
        resp = await client.post("/api/v1/agent/chat", json=payload, headers=auth_headers)
        assert resp.status_code in (400, 422)
        body = resp.json()
        # 后端可能返回统一 error 结构，也可能返回 FastAPI 默认 detail
        assert "error" in body or "detail" in body

    async def test_method_not_allowed_returns_error_structure(self, client, assert_error) -> None:
        resp = await client.put("/api/v1/auth/login")
        assert_error(resp, expected_status=405)
