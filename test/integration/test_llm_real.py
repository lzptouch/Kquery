import pytest


@pytest.mark.integration
class TestLLMReal:
    """使用真实 DashScope 模型进行 LLM 链路验证（.env.test 已配置 API Key）。"""

    async def test_is_llm_configured_true_with_real_key(self) -> None:
        import app.core.llm_client as llm_client

        assert llm_client.is_llm_configured("agent") is True, ".env.test 中 LLM_AGENT_API_KEY 未生效"

    async def test_real_chat_completion(self) -> None:
        import app.core.llm_client as llm_client

        result = await llm_client.call_openai_chat(
            model="qwen-turbo",
            messages=[{"role": "user", "content": "你好，请用一句话介绍自己"}],
        )
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0

    async def test_real_embedding(self) -> None:
        import app.core.llm_client as llm_client

        result = await llm_client.call_openai_embedding(
            texts=["测试文本"],
            model="text-embedding-v3",
        )
        assert isinstance(result, list)
        assert len(result) == 1
        assert isinstance(result[0], list)
        assert len(result[0]) > 0

    async def test_qa_agent_with_real_model(self) -> None:
        from app.agents.qa_agent import QAAgent

        agent = QAAgent()
        result = await agent.chat(
            user_input="什么是 Kquery",
            session_id="s-1",
            enable_rag=False,
        )
        assert "response" in result or "answer" in result or "content" in result
