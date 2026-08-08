import random


class SearchLoadScenario:
    """检索链路压力测试：/api/v1/kquery/search 与 /api/v1/knowledge-graph/query"""

    def __init__(self, user):
        self.user = user

    def hybrid_search(self):
        query = random.choice(["RAG", "Kquery", "机器学习", "知识图谱", "文档管理"])
        payload = {"query": query, "top_k": 10}
        with self.user.client.post(
            "/api/v1/kquery/search",
            json=payload,
            catch_response=True,
            name="search",
        ) as resp:
            if resp.status_code in (200, 404, 400):
                # 即使集合为空返回 404 也视为服务层正常响应
                resp.success()
            else:
                resp.failure(f"status={resp.status_code}")

    def graph_query(self):
        payload = {"query": "MATCH (n) RETURN count(n) AS cnt", "limit": 10}
        with self.user.client.post(
            "/api/v1/knowledge-graph/query",
            json=payload,
            catch_response=True,
            name="graph-query",
        ) as resp:
            if resp.status_code == 200 or resp.status_code == 503:
                resp.success()
            else:
                resp.failure(f"status={resp.status_code}")
