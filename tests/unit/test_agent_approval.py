"""写类工具的人工审批链（KQ-20 的最后一块）。

此前：写类工具（http_request / python_sandbox / mcp）在 `run_with_timeout` 里
被拦下并返回 `approval_required` 错误——**但没有任何人能批准它**：
`approved_tools` 只有读没有写，HITL 也不提供"批准"这个动作。
结果：写类工具永远不可用，而图把它当成普通失败继续走。

现在：图在**执行前**挂起（`reason=tool_approval_required`），
用户批准后 resume，写工具才真正执行。
"""
from langgraph.types import Command

from app.services.agent.checkpointer import _memory_saver
from app.services.agent.graph import build_agent_graph
from app.services.agent.state import initial_state
from app.services.agent.tools import DANGER_READ_ONLY, DANGER_WRITE, ToolRegistry, ToolResult
from app.services.llm import MockLLM

from tests.integration.test_stage6_agent import agent_prompt_builder
from tests.unit.test_agent_state_fixes import (
    ScriptedPlanner,
    ScriptedReflector,
    _evidence,
    _make_registry,
)


class FakeWriteTool:
    """写类工具（名字在危险表里 → 默认按 write 处理）。"""

    name = "http_request"
    description = "发起外部写请求"

    def __init__(self):
        self.calls: list = []

    def run(self, query: str, tenant_id: str, **kwargs) -> ToolResult:
        self.calls.append(query)
        return ToolResult(items=[_evidence("w#1")])


class FakeReadTool:
    name = "vector_search"
    description = "只读检索"

    def run(self, query: str, tenant_id: str, **kwargs) -> ToolResult:
        return ToolResult(items=[_evidence("c#1", 0.9)])


def _graph_with(tool, planner_steps=None):
    # 复用测试脚手架的注册表构造（它会登记全局 registry holder，
    # ScriptedPlanner 的 sanitize_steps 需要它）
    registry = _make_registry(tool)
    planner = ScriptedPlanner(
        planner_steps or [[{"description": "写一下", "tool": "http_request"}]]
    )
    reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
    graph = build_agent_graph(
        registry, planner, reflector, MockLLM(), agent_prompt_builder,
        checkpointer=_memory_saver(),
    )
    return graph, registry


class TestDangerLookup:
    def test_write_tool_by_name(self):
        reg = ToolRegistry(timeout_s=1)
        reg.register(FakeWriteTool())
        assert reg.danger_of_name("http_request") == DANGER_WRITE

    def test_read_tool_by_name(self):
        reg = ToolRegistry(timeout_s=1)
        reg.register(FakeReadTool())
        assert reg.danger_of_name("vector_search") == DANGER_READ_ONLY

    def test_unknown_name_is_write(self):
        """未知工具按最危险处理：默认放行未知来源工具是权限事故的常见成因。"""
        reg = ToolRegistry(timeout_s=1)
        assert reg.danger_of_name("mcp__whatever") == DANGER_WRITE


class TestApprovalInterrupt:
    def test_write_tool_waits_for_approval(self):
        tool = FakeWriteTool()
        graph, _ = _graph_with(tool)
        final = graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"thread_id": "run-approve-1"}},
        )
        assert "__interrupt__" in final, "写类工具执行前必须挂起等审批"
        payload = final["__interrupt__"][0].value
        assert payload["reason"] == "tool_approval_required"
        assert payload["tool"] == "http_request"
        assert "approve" in payload["actions"]
        assert tool.calls == [], "未批准前不得执行写工具"

    def test_approved_tool_executes(self):
        tool = FakeWriteTool()
        graph, _ = _graph_with(tool)
        graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"thread_id": "run-approve-2"}},
        )
        graph.invoke(
            Command(resume={"action": "approve"}),
            config={"configurable": {"thread_id": "run-approve-2"}},
        )
        assert tool.calls == ["写一下"], "批准后写工具必须真正执行"

    def test_rejected_tool_is_skipped(self):
        """人工拒绝：不执行工具，该步标记失败，整条链继续（不中断）。"""
        tool = FakeWriteTool()
        graph, _ = _graph_with(tool)
        graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"thread_id": "run-approve-3"}},
        )
        final = graph.invoke(
            Command(resume={"action": "reject"}),
            config={"configurable": {"thread_id": "run-approve-3"}},
        )
        assert tool.calls == [], "被拒绝的写工具不得执行"
        errors = final.get("tool_errors") or []
        assert any(e.get("error_class") == "approval_rejected" for e in errors)

    def test_pre_approved_tool_does_not_interrupt(self):
        """预审批（configurable.approved_tools）→ 不挂起，直接执行。"""
        tool = FakeWriteTool()
        graph, _ = _graph_with(tool)
        final = graph.invoke(
            initial_state("问题", "t1"),
            config={
                "configurable": {
                    "thread_id": "run-approve-4",
                    "approved_tools": {"http_request": True},
                }
            },
        )
        assert "__interrupt__" not in final
        assert tool.calls == ["写一下"]

    def test_read_tool_never_interrupts(self):
        """只读工具不该因为审批被打断（打断是最后手段，不是常规路径）。"""
        graph, _ = _graph_with(
            FakeReadTool(), planner_steps=[[{"description": "查一下", "tool": "vector_search"}]]
        )
        final = graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"thread_id": "run-approve-5"}},
        )
        assert "__interrupt__" not in final


class TestApprovalStore:
    def test_approve_and_read(self):
        from app.services.agent.runner import AgentRunner

        runner = AgentRunner.__new__(AgentRunner)
        import threading

        runner._approved_lock = threading.Lock()
        runner._approved_tools = {}
        assert runner.approved_tools("r1") == {}
        runner.approve_tools("r1", ["http_request"])
        assert runner.approved_tools("r1") == {"http_request": True}

    def test_revoke(self):
        from app.services.agent.runner import AgentRunner
        import threading

        runner = AgentRunner.__new__(AgentRunner)
        runner._approved_lock = threading.Lock()
        runner._approved_tools = {}
        runner.approve_tools("r1", ["http_request"])
        runner.approve_tools("r1", ["http_request"], approved=False)
        assert runner.approved_tools("r1") == {}

    def test_clear(self):
        from app.services.agent.runner import AgentRunner
        import threading

        runner = AgentRunner.__new__(AgentRunner)
        runner._approved_lock = threading.Lock()
        runner._approved_tools = {}
        runner.approve_tools("r1", ["http_request"])
        runner.clear_approvals("r1")
        assert runner.approved_tools("r1") == {}
