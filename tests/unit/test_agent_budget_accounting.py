"""Agent 预算记账修复的验证测试（KQ-18）。

对应改造：
- 规划/反思阶段的 token 一律按常量估算 → 与真实用量脱钩，`agent_token_budget`
  只对最后一段（answer）真实生效。现改为图上报 `llm.last_usage`，由 runner 统一记账。
- answer 阶段最贵，却**没有回填预算**（只进指标与账单）→ 预算账本漏记大头。
- answer 生成前没有闸门 → "生成完才发现超限"等于钱已经花了。
- resume 时新建账本会把 deadline 重新计时 → 反复 resume 即可无限延长时限。
"""
from types import SimpleNamespace

from app.services.agent.budget import AgentBudget
from app.services.agent.graph import build_agent_graph
from app.services.agent.runner import AgentRunner
from app.services.agent.state import initial_state
from app.services.llm import MockLLM

from tests.integration.test_stage6_agent import FakeSearchTool, agent_prompt_builder
from tests.unit.test_agent_state_fixes import (
    ScriptedPlanner,
    ScriptedReflector,
    _evidence,
    _make_registry,
)


class _CountingLLM:
    """记录是否被调用过（断言"预算不足时不生成"）。"""

    def __init__(self):
        self.calls = 0
        self._inner = MockLLM()

    def generate(self, prompt: str) -> str:
        self.calls += 1
        return self._inner.generate(prompt)

    def stream(self, prompt: str):
        self.calls += 1
        yield from self._inner.stream(prompt)

    @property
    def last_usage(self):
        return self._inner.last_usage


def _graph(llm=None):
    registry = _make_registry(FakeSearchTool([_evidence("c#1", 0.9)]))
    planner = ScriptedPlanner([[{"description": "查", "tool": "vector_search"}]])
    reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
    return build_agent_graph(
        registry, planner, reflector, llm or MockLLM(), agent_prompt_builder
    )


class TestAnswerBudgetGate:
    def test_answer_skipped_when_budget_insufficient(self):
        """额度不够一次生成时不得开始生成（钱花了才发现超限 = 闸门形同虚设）。"""
        llm = _CountingLLM()
        graph = _graph(llm)
        # token_limit 远小于 ANSWER_TOKEN_RESERVE → 生成前就该被拦住
        budget = AgentBudget(token_limit=100)
        final = graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"budget": budget}},
        )
        assert final["status"] == "budget_exhausted"
        assert final["incomplete"] is True
        assert final["fuse_reason"] == "budget_token"
        assert llm.calls == 0, "预算不足时不得调用 LLM 生成答案"

    def test_answer_runs_when_budget_sufficient(self):
        llm = _CountingLLM()
        graph = _graph(llm)
        final = graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"budget": AgentBudget(token_limit=100000)}},
        )
        assert final.get("status") != "budget_exhausted"
        assert llm.calls >= 1


class TestBudgetRestore:
    """resume 必须继承已消耗的时间，否则 deadline 可以被无限延长。"""

    def _runner_stub(self, values: dict):
        snapshot = SimpleNamespace(values=values)

        class FakeGraph:
            def get_state(self, config):
                return snapshot

        return SimpleNamespace(_settings=None, _graph=FakeGraph())

    def test_elapsed_is_inherited(self):
        stub = self._runner_stub({"budget": {"tokens_used": 100, "cost_used_yuan": 0.1, "elapsed_ms": 5000}})
        budget = AgentRunner._restore_budget(stub, "run-1")
        assert budget.tokens_used == 100
        assert budget.cost_used == 0.1
        assert budget.elapsed_ms >= 5000, "已消耗的时间必须继承（deadline 不能因 resume 重置）"

    def test_no_saved_budget_is_fresh(self):
        stub = self._runner_stub({})
        budget = AgentRunner._restore_budget(stub, "run-1")
        assert budget.tokens_used == 0
        assert budget.elapsed_ms < 1000

    def test_tenant_restored_from_checkpoint(self):
        stub = self._runner_stub({"tenant_id": "t-42"})
        assert AgentRunner._restore_tenant(stub, "run-1") == "t-42"
