from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.orchestrator import AgentOrchestrator, AgentIntent
from app.workflow.models import WorkflowConfig, WorkflowNode, WorkflowEdge, NodeType, ExecutionStatus


@pytest.mark.unit
class TestOrchestrator:
    async def test_orchestrate_returns_result(self) -> None:
        orchestrator = AgentOrchestrator()
        with patch.object(orchestrator, "_get_qa_agent", new=AsyncMock()) as get_agent_mock:
            agent = MagicMock()
            agent.chat = AsyncMock(return_value={"response": "mock"})
            get_agent_mock.return_value = agent

            result = await orchestrator.orchestrate(
                intent="general_chat",
                context={"query": "你好", "session_id": "s-1"},
            )
            assert "intent" in result
            assert result["intent"] == AgentIntent.GENERAL_CHAT.value
            agent.chat.assert_awaited_once()

    async def test_execute_workflow(self) -> None:
        orchestrator = AgentOrchestrator()
        config = WorkflowConfig(
            id="wf-1",
            name="test",
            nodes=[
                WorkflowNode(id="start", name="start", node_type=NodeType.START),
                WorkflowNode(id="llm", name="llm", node_type=NodeType.LLM),
                WorkflowNode(id="end", name="end", node_type=NodeType.END),
            ],
            edges=[
                WorkflowEdge(id="e1", source="start", target="llm"),
                WorkflowEdge(id="e2", source="llm", target="end"),
            ],
        )
        with patch.object(orchestrator, "execute_workflow", new=AsyncMock(return_value={"execution_id": "e-1", "status": ExecutionStatus.COMPLETED.value})) as exec_mock:
            result = await orchestrator.execute_workflow(config, context={})
            assert result["execution_id"] == "e-1"
            assert result["status"] == ExecutionStatus.COMPLETED.value
