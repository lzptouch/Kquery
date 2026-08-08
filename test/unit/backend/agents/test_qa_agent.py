from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.qa_agent import QAAgent


@pytest.mark.unit
class TestQAAgent:
    async def test_init_creates_agent_graph(self) -> None:
        with patch("app.core.llm_client.is_llm_configured", return_value=False):
            agent = QAAgent()
            assert agent.agent_graph is not None
            assert agent.enable_rag is True

    async def test_chat_general_without_rag(self) -> None:
        dummy_tool = MagicMock()
        dummy_tool.name = "dummy"
        with patch("app.core.llm_client.is_llm_configured", return_value=False), \
             patch("app.agents.qa_agent.QAAgent._initialize_tools", return_value=[dummy_tool]), \
             patch("app.agents.qa_agent.QAAgent._create_agent_graph", return_value=MagicMock()), \
             patch("app.core.llm_client.call_openai_chat", new=AsyncMock(return_value="mock answer")):
            agent = QAAgent()
            result = await agent.chat(
                user_input="你好",
                session_id="s-1",
                collection_name="",
                enable_rag=False,
            )
            assert "response" in result or "answer" in result


@pytest.mark.unit
class TestQAAgentTools:
    async def test_init_without_rag_has_no_rag_tools(self) -> None:
        dummy_tool = MagicMock()
        dummy_tool.name = "dummy"
        with patch("app.core.llm_client.is_llm_configured", return_value=False), \
             patch("app.agents.qa_agent.QAAgent._initialize_tools", return_value=[dummy_tool]), \
             patch("app.agents.qa_agent.QAAgent._create_agent_graph", return_value=MagicMock()):
            agent = QAAgent(enable_rag=False)
            assert len(agent.tools) == 1

    async def test_init_with_rag_creates_agent(self) -> None:
        dummy_tool = MagicMock()
        dummy_tool.name = "dummy"
        # 避免拉取真实 RAG 工具，mock _initialize_tools 和 create_react_agent
        with patch("app.core.llm_client.is_llm_configured", return_value=False), \
             patch("app.agents.qa_agent.QAAgent._initialize_tools", return_value=[dummy_tool]), \
             patch("app.agents.qa_agent.create_react_agent", return_value=MagicMock()) as mock_create:
            agent = QAAgent(enable_rag=True)
            assert agent.agent_graph is not None
            mock_create.assert_called_once()
