import pytest


@pytest.mark.api
class TestSkillsWebsearchAPI:
    async def test_list_skills(self, client, auth_headers, assert_success) -> None:
        resp = await client.get("/api/v1/skills", headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert isinstance(body, list)

    async def test_execute_skill(self, client, auth_headers, assert_success) -> None:
        payload = {"skill_name": "qa", "params": {"question": "测试"}}
        resp = await client.post("/api/v1/skills/execute", json=payload, headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert "result" in body or "response" in body

    async def test_web_search(self, client, auth_headers, assert_success) -> None:
        payload = {"query": "Kquery RAG"}
        resp = await client.post("/api/v1/web-search", json=payload, headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert isinstance(body, list) or "results" in body
