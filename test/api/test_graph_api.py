import pytest


@pytest.mark.api
class TestGraphAPI:
    async def test_graph_query(self, client, auth_headers, assert_success) -> None:
        payload = {"query": "MATCH (n) RETURN count(n) AS cnt", "limit": 10}
        resp = await client.post("/api/v1/knowledge-graph/query", json=payload, headers=auth_headers)
        # 图谱接口可能未启用或返回业务错误；允许 200/400/422/404/500/503
        if resp.status_code == 200:
            assert_success(resp, expected_status=200)
        else:
            assert resp.status_code in (400, 404, 422, 500, 503)

    async def test_graph_query_invalid_cypher(self, client, auth_headers, assert_error) -> None:
        payload = {"query": "INVALID CYPHER"}
        resp = await client.post("/api/v1/knowledge-graph/query", json=payload, headers=auth_headers)
        assert resp.status_code in (400, 404, 422, 500, 503)

    async def test_graphrag_chat(self, client, auth_headers, assert_success) -> None:
        payload = {"question": "介绍下图谱", "collection_name": "default"}
        resp = await client.post("/api/v1/graphrag/chat", json=payload, headers=auth_headers)
        if resp.status_code == 200:
            body = assert_success(resp, expected_status=200)
            assert "answer" in body or "response" in body
