import pytest


@pytest.mark.api
class TestHealthOpsAPI:
    async def test_health(self, client, assert_success) -> None:
        resp = await client.get("/health")
        body = assert_success(resp, expected_status=200)
        assert body.get("status") in ("ok", "healthy", "success") or "status" in body

    async def test_metrics(self, client, assert_success) -> None:
        resp = await client.get("/metrics")
        # Prometheus 指标仅在 enable_metrics=True 时挂载；允许 200 或 404
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            text = resp.text
            assert "http_request" in text or "fastapi" in text or "# TYPE" in text

    async def test_docs(self, client, assert_success) -> None:
        resp = await client.get("/docs")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")
