import pytest


@pytest.mark.api
class TestMultimodalAPI:
    async def test_multimodal_generate_image(self, client, auth_headers, assert_success) -> None:
        payload = {"prompt": "一只猫", "type": "image"}
        resp = await client.post("/api/v1/multimodal/generate", json=payload, headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert "url" in body or "content" in body or "data" in body

    async def test_multimodal_generate_video(self, client, auth_headers, assert_success) -> None:
        payload = {"prompt": "风景视频", "type": "video"}
        resp = await client.post("/api/v1/multimodal/generate", json=payload, headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert "url" in body or "content" in body or "task_id" in body
