from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.llm_client as llm_client
from app.core.mock_data import get_mock_data


@pytest.mark.unit
class TestLLMClientCalls:
    async def test_openai_call_with_real_model(self) -> None:
        result = await llm_client.call_openai_chat(
            model="qwen-turbo",
            messages=[{"role": "user", "content": "你好，请用一句话介绍自己"}],
        )
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0

    async def test_dashscope_call_with_real_model(self) -> None:
        result = await llm_client.call_dashscope(
            model="qwen-turbo",
            messages=[{"role": "user", "content": "你好，请用一句话介绍自己"}],
        )
        assert result is not None
        assert isinstance(result, str)
        assert len(result) > 0


@pytest.mark.unit
class TestLLMConfigured:
    def test_is_llm_configured_true_with_env_key(self) -> None:
        assert llm_client.is_llm_configured("agent") is True, ".env.test 中 LLM_AGENT_API_KEY 未生效"

    def test_is_llm_configured_false_without_key(self) -> None:
        settings = MagicMock()
        settings.llm.agent_api_key = None
        settings.llm.embedding_api_key = None
        settings.llm.agent_api_base = None
        settings.llm.embedding_api_base = None
        with patch.object(llm_client, "get_settings", return_value=settings):
            assert llm_client.is_llm_configured("agent") is False


@pytest.mark.unit
class TestMockData:
    def test_get_mock_qa_answer(self) -> None:
        data = get_mock_data("qa_answer", question="测试")
        assert isinstance(data, str) and "测试" in data

    def test_get_mock_embeddings(self) -> None:
        data = get_mock_data("embeddings", texts=["测试"])
        assert isinstance(data, list)
        assert len(data) == 1

    def test_get_mock_unknown_module(self) -> None:
        with pytest.raises(ValueError):
            get_mock_data("non_existent_module")
