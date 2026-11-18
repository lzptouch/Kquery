"""工具空结果的降级链（KQ-19 的最后一块：graph_search 空 → vector_search）。

此前 `graph_search` 返回 0 条即判该步失败：但"库里没有这个实体"是**正常业务结果**，
不是工具故障。直接判失败会让 Agent 白丢一次机会（只能重规划或拒答）。
现在：0 条且无错误 → 按配置的映射降级到兜底工具，命中即算这一步成功。
"""
from app.services.agent.graph import build_agent_graph
from app.services.agent.state import initial_state
from app.services.agent.tools import ToolResult
from app.services.llm import MockLLM

from tests.integration.test_stage6_agent import agent_prompt_builder
from tests.unit.test_agent_state_fixes import (
    ScriptedPlanner,
    ScriptedReflector,
    _evidence,
    _make_registry,
)


class EmptyGraphTool:
    """图谱检索：库里没有这个实体 → 0 条（正常业务结果，不是故障）。"""

    name = "graph_search"
    description = "图谱检索"

    def run(self, query: str, tenant_id: str, **kwargs) -> ToolResult:
        return ToolResult(items=[])


class HitVectorTool:
    name = "vector_search"
    description = "语义检索"

    def run(self, query: str, tenant_id: str, **kwargs) -> ToolResult:
        return ToolResult(items=[_evidence("c#1", 0.8)])


def _run_with_fallbacks(fallbacks: dict, tool=EmptyGraphTool(), tool_name="graph_search"):
    registry = _make_registry(tool)
    registry.register(HitVectorTool())
    planner = ScriptedPlanner([[{"description": "查一下", "tool": tool_name}]])
    reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
    graph = build_agent_graph(registry, planner, reflector, MockLLM(), agent_prompt_builder)
    final = graph.invoke(
        initial_state("问题", "t1"),
        config={"configurable": {"thread_id": "run-fb-1", "tool_fallbacks": fallbacks}},
    )
    return final


class TestEmptyResultFallback:
    def test_fallback_recovers_empty_graph_search(self):
        final = _run_with_fallbacks({"graph_search": "vector_search"})
        assert final["status"] not in ("failed",), "降级命中后不该整步失败"
        calls = final.get("tool_calls") or []
        assert calls, "应有工具调用记录"
        assert calls[0]["tool_name"] == "vector_search", "降级后记录的工具应是兜底工具"
        assert calls[0].get("fallback_from") == "graph_search"
        assert len(final.get("evidence") or []) == 1, "兜底命中的证据要进上下文"

    def test_no_fallback_keeps_old_behaviour(self):
        """没配降级映射时行为不变：0 条即失败（不静默改语义）。"""
        final = _run_with_fallbacks({})
        errors = final.get("tool_errors") or []
        assert any(e.get("error") == "无结果（0 条）" for e in errors)

    def test_fallback_not_used_when_first_tool_has_results(self):
        """首路有结果时不得触发降级（否则多烧一次上游调用）。"""
        final = _run_with_fallbacks(
            {"graph_search": "vector_search"}, tool=HitVectorTool(), tool_name="graph_search"
        )
        calls = final.get("tool_calls") or []
        assert len(calls) == 1
        assert not calls[0].get("fallback_from")


class TestFallbacksConfig:
    def test_default_mapping(self):
        from app.core.config import Settings
        from app.services.agent.runner import tool_fallbacks

        assert tool_fallbacks(Settings()) == {"graph_search": "vector_search"}

    def test_broken_json_is_ignored(self):
        from app.services.agent.runner import tool_fallbacks

        class S:
            agent_tool_fallbacks_json = "not json"

        assert tool_fallbacks(S()) == {}

    def test_non_dict_is_ignored(self):
        from app.services.agent.runner import tool_fallbacks

        class S:
            agent_tool_fallbacks_json = "[1, 2]"

        assert tool_fallbacks(S()) == {}
