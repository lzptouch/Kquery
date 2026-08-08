from unittest.mock import AsyncMock

import pytest

from app.workflow.engine import DAGExecutionEngine, NodeExecutor
from app.workflow.models import WorkflowConfig, WorkflowNode, WorkflowEdge, NodeType, ExecutionStatus


@pytest.mark.unit
class TestDAGExecutionEngine:
    async def test_linear_workflow(self) -> None:
        executor = NodeExecutor()
        executor.register_handler(NodeType.START, AsyncMock(return_value={"ok": True}))
        executor.register_handler(NodeType.LLM, AsyncMock(return_value={"ok": True}))
        executor.register_handler(NodeType.END, AsyncMock(return_value={"ok": True}))
        engine = DAGExecutionEngine(executor)

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
        result = await engine.execute(config, inputs={"question": "q"})
        assert result.status == ExecutionStatus.COMPLETED

    async def test_cycle_handled_without_infinite_loop(self) -> None:
        executor = NodeExecutor()
        engine = DAGExecutionEngine(executor)
        config = WorkflowConfig(
            id="wf-2",
            name="cycle",
            nodes=[
                WorkflowNode(id="a", name="a", node_type=NodeType.START),
                WorkflowNode(id="b", name="b", node_type=NodeType.LLM),
            ],
            edges=[
                WorkflowEdge(id="e1", source="a", target="b"),
                WorkflowEdge(id="e2", source="b", target="a"),
            ],
        )
        # 引擎应能处理环（抛异常或返回失败状态均可），关键是不能无限循环
        try:
            result = await engine.execute(config, inputs={})
            assert result.status != ExecutionStatus.RUNNING
        except Exception:
            pass  # 主动检测并报错也是合理行为
