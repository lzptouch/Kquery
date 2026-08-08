import random


class ChatLoadScenario:
    """聊天/问答链路压力测试：/api/v1/agent/chat"""

    def __init__(self, user):
        self.user = user

    def chat_with_rag(self):
        payload = {
            "query": "什么是 RAG",
            "session_id": f"s-stress-{random.randint(1, 100000)}",
            "collection_name": "",
            "enable_rag": False,
        }
        with self.user.client.post(
            "/api/v1/agent/chat",
            json=payload,
            catch_response=True,
            name="chat",
        ) as resp:
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") or "response" in data:
                    resp.success()
                else:
                    resp.failure("success=false")
            else:
                resp.failure(f"status={resp.status_code}")

    def chat_stream(self):
        payload = {
            "query": "流式测试",
            "session_id": f"s-stream-{random.randint(1, 100000)}",
            "collection_name": "",
            "enable_rag": False,
            "stream": True,
        }
        with self.user.client.post(
            "/api/v1/agent/chat",
            json=payload,
            catch_response=True,
            name="chat-stream",
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"status={resp.status_code}")
