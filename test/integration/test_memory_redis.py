import pytest


@pytest.mark.integration
class TestMemoryRedis:
    async def test_memory_write_and_read(self, test_env: dict) -> None:
        import redis.asyncio as redis

        r = redis.from_url(test_env["REDIS_URL"])
        await r.set("test:memory:1", "hello")
        value = await r.get("test:memory:1")
        assert value.decode() == "hello"
        await r.delete("test:memory:1")
        await r.close()

    async def test_memory_ttl(self, test_env: dict) -> None:
        import asyncio
        import redis.asyncio as redis

        r = redis.from_url(test_env["REDIS_URL"])
        await r.setex("test:memory:ttl", 1, "v")
        await asyncio.sleep(1.2)
        value = await r.get("test:memory:ttl")
        assert value is None
        await r.close()

    async def test_memory_service_fact_lifecycle(self, mock_llm_client: dict) -> None:
        from app.services.long_term_memory_service import LongTermMemoryService

        service = LongTermMemoryService()
        messages = [
            {"role": "user", "content": "我喜欢用 RAG 做知识问答"},
            {"role": "assistant", "content": "好的，我会记住您的偏好。"},
        ]
        facts = await service.extract_facts(messages, user_id="user-1")
        # 无真实 LLM 时抽取结果可能为空，但不应抛异常
        assert isinstance(facts, list)
        if facts:
            await service.store_fact("user-1", facts[0], "session-1")
            retrieved = await service.retrieve_facts("user-1", "RAG")
            assert isinstance(retrieved, list)
