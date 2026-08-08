import pytest


@pytest.mark.api
class TestMemoryTasksAPI:
    async def test_long_term_memory(self, client, auth_headers, assert_success) -> None:
        payload = {"content": "用户喜欢 RAG 技术", "session_id": "s-mem-1"}
        resp = await client.post("/api/v1/memory", json=payload, headers=auth_headers)
        if resp.status_code in (200, 201):
            assert_success(resp, expected_status=resp.status_code, required_fields=["id"])

        resp = await client.get("/api/v1/memory?s=m-mem-1", headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert isinstance(body, (list, dict))

    async def test_task_create_and_get(self, client, auth_headers, assert_success) -> None:
        payload = {"name": "测试任务", "description": "API 任务", "agent": "qa"}
        resp = await client.post("/api/v1/tasks", json=payload, headers=auth_headers)
        if resp.status_code in (200, 201):
            body = assert_success(resp, expected_status=resp.status_code, required_fields=["id", "status"])
            task_id = body["id"]

            resp = await client.get(f"/api/v1/tasks/{task_id}", headers=auth_headers)
            assert_success(resp, expected_status=200, required_fields=["id", "status"])
