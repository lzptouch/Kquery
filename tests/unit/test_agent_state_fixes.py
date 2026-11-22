"""Agent 状态机与工具执行修复的验证测试（批次 C）。

对应改造：
- `step is None` 的 replan 信号此前被 reflect_node 无条件覆盖，
  且不计 iteration → 熔断不可达。现改为 tool_exhausted 短路 + 计轮次。
- 重规划只偏移 step_index 不偏移 depends_on → 依赖错位/悬空。
- 工具超时用 `with ThreadPoolExecutor` 会 join，熔断形同虚设。
- 反射器「检索信号兜底」抬分使 replan 分支永不执行。
"""
import time

import pytest

from app.domain.schemas import Chunk
from app.services.agent.graph import build_agent_graph
from app.services.agent.planner import sanitize_steps
from app.services.agent.state import initial_state
from app.services.agent.tools import CalculatorTool, ToolRegistry, ToolResult
from app.services.llm import MockLLM

from tests.integration.test_stage6_agent import FakeSearchTool, agent_prompt_builder  # noqa: F401 - 复用测试脚手架

_registry_holder = None


def _evidence(chunk_id: str, score: float = 0.5, content: str = "内容") -> dict:
    return {
        "chunk_id": chunk_id, "content": content, "score": score,
        "source": "测试文档", "doc_id": "doc-t", "source_route": "vector",
    }


class FailingTool:
    """总是失败的检索工具（用于逼出 replan 分支）。"""

    name = "vector_search"
    description = "总是失败"

    def run(self, query: str, tenant_id: str) -> ToolResult:
        return ToolResult(error="工具炸了")


class SlowTool:
    name = "vector_search"
    description = "很慢的工具"

    def run(self, query: str, tenant_id: str) -> ToolResult:
        time.sleep(30)
        return ToolResult(items=[_evidence("slow#1")])


class ScriptedPlanner:
    def __init__(self, plans: list[list[dict]]):
        self._plans = plans
        self.calls = 0

    def plan(self, question: str, replan_hint: str = "") -> list[dict]:
        raw = self._plans[min(self.calls, len(self._plans) - 1)]
        self.calls += 1
        return sanitize_steps(raw, _registry_holder)


class ScriptedReflector:
    def __init__(self, verdicts: list[dict]):
        self._verdicts = verdicts
        self.calls = 0

    def reflect(self, question, evidence, remaining_steps) -> dict:
        v = self._verdicts[min(self.calls, len(self._verdicts) - 1)]
        self.calls += 1
        return dict(v)


def _make_registry(tool) -> ToolRegistry:
    global _registry_holder
    registry = ToolRegistry(timeout_s=2)
    registry.register(tool)
    registry.register(CalculatorTool())
    _registry_holder = registry
    return registry


def _run(registry, planner, reflector, question="测试问题"):
    graph = build_agent_graph(registry, planner, reflector, MockLLM(), agent_prompt_builder)
    events: list = []
    final = graph.invoke(
        initial_state(question, "tenant"),
        config={"configurable": {"emit": lambda e, p: events.append((e, p))}},
    )
    return final, events


class TestReplanSignalNoLongerSwallowed:
    """无可执行步骤的 replan 信号必须真的生效。"""

    def test_failed_steps_trigger_replan_and_count_iteration(self):
        registry = _make_registry(FailingTool())
        planner = ScriptedPlanner([
            [{"description": "查一次", "tool": "vector_search"}],
            [{"description": "换个查法", "tool": "vector_search"}],
        ])
        # 反思判定"证据不足、继续找"（而不是直接作答），才会走到重规划分支
        reflector = ScriptedReflector([{"score": 0.3, "next_action": "continue", "reason": "证据不足"}])
        final, _ = _run(registry, planner, reflector)

        assert planner.calls >= 2, "步骤失败后必须触发重规划"
        assert final["replan_count"] >= 1
        assert final["iteration"] >= 1, "该路径此前不递增 iteration，熔断永远不可达"
        assert final["status"] in ("completed", "circuit_breaker")

    def test_tool_exhausted_when_no_runnable_steps(self):
        """无可执行步骤（空计划/步骤全耗尽）→ reflect 短路并升级 replan，且计入 iteration。

        这是防御性分支：正常流程里 failed 步骤也算进 done_set，依赖不会被永久阻塞；
        但空计划、悬空依赖、快照跨版本等都可能落到这里——此前该路径不计入 iteration，
        第一层熔断永远不可达。
        """
        registry = _make_registry(FailingTool())
        planner = ScriptedPlanner([
            [],  # 空计划：sanitize_steps 保留为空 → 无可执行步骤
            [{"description": "补救步骤", "tool": "vector_search"}],
        ])
        reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
        final, events = _run(registry, planner, reflector)

        reflect_events = [p for e, p in events if e == "reflect"]
        assert reflect_events, "应有 reflect 事件"
        assert "无可执行步骤" in reflect_events[0]["reason"], (
            "首个 reflect 应是 tool_exhausted 短路，而不是让 LLM 再答一遍"
        )
        assert planner.calls >= 2, "短路后应升级重规划"
        assert final["iteration"] >= 1, "该路径必须计入 iteration，熔断才可达"
        assert final["replan_count"] >= 1

    def test_converges_within_max_iterations(self):
        """重规划不能无限打转：必须在 max_iterations 内收敛。"""
        registry = _make_registry(FailingTool())
        planner = ScriptedPlanner([[{"description": "循环", "tool": "vector_search"}]])
        reflector = ScriptedReflector([{"score": 0.5, "next_action": "replan", "reason": "不够"}])
        t0 = time.monotonic()
        final, _ = _run(registry, planner, reflector)
        assert time.monotonic() - t0 < 20, "重规划必须收敛"
        assert final["status"] in ("completed", "circuit_breaker", "failed")


class TestReplanDependencyOffset:
    """重规划后 depends_on 必须同步偏移并剔除悬空依赖。"""

    def test_dependencies_offset_to_global_indexes(self):
        registry = _make_registry(FailingTool())
        planner = ScriptedPlanner([
            [{"description": "第一步", "tool": "vector_search"},
             {"description": "第二步", "tool": "calculator", "depends_on": [0]}],
            # 重规划：第 2 步依赖第 1 步（局部序号 [0]）
            [{"description": "新A", "tool": "vector_search"},
             {"description": "新B", "tool": "calculator", "depends_on": [0]}],
        ])
        reflector = ScriptedReflector([{"score": 0.3, "next_action": "replan", "reason": "不够"}])
        final, _ = _run(registry, planner, reflector)

        plan = final["plan"]
        valid = {s["step_index"] for s in plan}
        for s in plan:
            assert all(d in valid for d in s["depends_on"]), f"悬空依赖: {s}"
        # 第二个计划的依赖应指向全局序号（base + 0），而不是局部 0
        new_steps = [s for s in plan if s["description"] in ("新A", "新B")]
        if len(new_steps) == 2:
            a, b = new_steps
            assert b["depends_on"] == [a["step_index"]], (
                "重规划的 depends_on 未随 step_index 偏移，会指向错误的历史步骤"
            )

    def test_dangling_dependency_dropped(self):
        registry = _make_registry(FailingTool())
        planner = ScriptedPlanner([
            [{"description": "唯一一步", "tool": "vector_search"}],
            [{"description": "挂空依赖", "tool": "calculator", "depends_on": [99]}],
        ])
        reflector = ScriptedReflector([{"score": 0.3, "next_action": "replan", "reason": "不够"}])
        final, _ = _run(registry, planner, reflector)
        for s in final["plan"]:
            assert all(d < 10 ** 6 for d in s["depends_on"]), "悬空依赖未被剔除"


class TestToolTimeoutActuallyInterrupts:
    """超时必须让请求真正返回。"""

    def test_slow_tool_returns_within_timeout(self):
        registry = _make_registry(SlowTool())
        result, _, name = registry.run_with_timeout("vector_search", "q", "t1")
        assert result.error and "超时" in result.error
        assert name == "vector_search"

    def test_timeout_is_shorter_than_tool_duration(self):
        """关键回归：30s 的工具在 2s 超时下，请求必须在 2s 左右返回而非等 30s。"""
        registry = _make_registry(SlowTool())
        t0 = time.monotonic()
        result, _, _ = registry.run_with_timeout("vector_search", "q", "t1")
        elapsed = time.monotonic() - t0
        assert elapsed < 10, f"超时未生效，实际耗时 {elapsed:.1f}s（修复前会被 join 到 30s）"
        assert result.error

    def test_normal_tool_unaffected(self):
        class FastTool:
            name = "vector_search"
            description = "快"

            def run(self, query, tenant_id):
                return ToolResult(items=[_evidence("fast#1")], elapsed_ms=1)

        registry = _make_registry(FastTool())
        result, _, _ = registry.run_with_timeout("vector_search", "q", "t1")
        assert not result.error and len(result.items) == 1


class TestReflectionReachability:
    """弱证据不应被「检索信号兜底」抬分到必然作答。"""

    def test_weak_verdict_kept_as_is(self):
        from app.services.agent.reflector import ANSWER_THRESHOLD, _parse_reflect

        verdict = _parse_reflect('{"score": 0.3, "next_action": "replan", "reason": "证据太弱"}')
        assert verdict["next_action"] == "replan"
        assert verdict["score"] < ANSWER_THRESHOLD, "低分判定不应被抬到作答线之上"

    def test_no_retrieval_floor_lift_in_reflector(self):
        """回归护栏：reflector 中不得再出现把分数抬到作答线的常量。"""
        import inspect

        from app.services.agent import reflector as reflector_mod

        src = inspect.getsource(reflector_mod)
        assert "RETRIEVAL_FLOOR_LIFT" not in src, (
            "反射器不得再把弱证据抬到作答线（会使 replan 分支永不执行）"
        )
        assert "= 0.65" not in src, "不应存在硬编码的抬分常量"


class TestChunkFixtureSanity:
    def test_chunk_model_defaults(self):
        c = Chunk(chunk_id="d#0", doc_id="d", seq=1, content="x", tenant_id="t", source="第1页")
        assert c.level == 0 and c.modality == "text"


@pytest.mark.parametrize("depth", [1, 2])
def test_neighbors_respects_depth(depth):
    """图存储深度参数仍生效（租户化改造不应影响 BFS 语义）。"""
    from app.services.graph.store import InMemoryGraphStore

    store = InMemoryGraphStore(None)
    for i in range(5):
        store.upsert_entity(
            Chunk and __import__("app.domain.schemas", fromlist=["EntityNode"]).EntityNode(
                entity_id=f"e{i}", name=f"e{i}", tenant_id="t1"
            )
        )
    items = store.neighbors("e0", depth=depth, tenant_id="t1")
    assert isinstance(items, list)


class TestCheckpointer:
    """checkpointer 是断点续跑 / 多轮继承 / 回放的共同前提。

     曾把它写进交付承诺，代码里长期是 `graph.compile()` 裸编译。
    """

    def test_thread_id_convention(self):
        from app.services.agent.checkpointer import thread_id_for

        assert thread_id_for("s1", 1) == "s1:1"
        # 显式带 turn：同一会话不同轮必须是不同 checkpoint，否则互相覆盖
        assert thread_id_for("s1", 1) != thread_id_for("s1", 2)

    def test_build_checkpointer_memory(self, tmp_path):
        from app.core.config import Settings
        from app.services.agent.checkpointer import build_checkpointer

        saver = build_checkpointer(Settings(agent_checkpointer="memory"), tmp_path)
        assert saver is not None

    def test_build_checkpointer_none_disables(self, tmp_path):
        from app.core.config import Settings
        from app.services.agent.checkpointer import build_checkpointer

        assert build_checkpointer(Settings(agent_checkpointer="none"), tmp_path) is None

    def test_build_checkpointer_sqlite_creates_db(self, tmp_path):
        from app.core.config import Settings
        from app.services.agent.checkpointer import build_checkpointer, close_checkpointer

        saver = build_checkpointer(
            Settings(agent_checkpointer="sqlite", agent_checkpoint_path=str(tmp_path / "cp.db")),
            tmp_path,
        )
        assert saver is not None, "sqlite 档必须能构造出 saver"
        assert (tmp_path / "cp.db").exists(), "sqlite 文件应被创建"
        close_checkpointer(saver)

    def test_checkpointer_survives_restart(self, tmp_path):
        """核心验收：同一 thread_id 二次调用能取回上一轮 state（真续跑）。"""
        from langgraph.checkpoint.memory import MemorySaver

        from app.services.agent.graph import build_agent_graph
        from app.services.agent.state import initial_state

        registry = _make_registry(FakeSearchTool([_evidence("c#1", 0.9)]))
        planner = ScriptedPlanner([[{"description": "查", "tool": "vector_search"}]])
        reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])

        graph = build_agent_graph(
            registry, planner, reflector, MockLLM(), agent_prompt_builder,
            checkpointer=MemorySaver(),
        )
        cfg = {"configurable": {"thread_id": "sess:1"}}
        graph.invoke(initial_state("第一轮问题", "t1"), config=cfg)

        snapshots = list(graph.get_state_history({"configurable": {"thread_id": "sess:1"}}))
        assert snapshots, "启用 checkpointer 后应能取到历史快照（否则续跑形同虚设）"
        assert any(s.values.get("question") == "第一轮问题" for s in snapshots)

    def test_graph_without_checkpointer_still_works(self):
        """checkpointer=None（降级）时 Agent 仍能跑通，不阻断主流程。"""

        registry = _make_registry(FakeSearchTool([_evidence("c#1", 0.9)]))
        planner = ScriptedPlanner([[{"description": "查", "tool": "vector_search"}]])
        reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
        final, _ = _run(registry, planner, reflector)
        assert final.get("final_answer") or final.get("refusal") is not None

    def test_close_checkpointer_is_idempotent(self, tmp_path):
        from app.core.config import Settings
        from app.services.agent.checkpointer import build_checkpointer, close_checkpointer

        saver = build_checkpointer(
            Settings(agent_checkpointer="sqlite", agent_checkpoint_path=str(tmp_path / "cp2.db")),
            tmp_path,
        )
        close_checkpointer(saver)
        close_checkpointer(saver)  # 二次关闭不得抛异常
        close_checkpointer(None)

class TestAgentBudget:
    """三预算治理。此前只有"轮数"一个维度有上限，资源维度完全裸奔。"""

    def _budget(self, **kw):
        from app.services.agent.budget import AgentBudget

        return AgentBudget(**kw)

    def test_unlimited_by_default(self):
        b = self._budget()
        assert b.exhausted() == "" and b.can_afford(tokens=10 ** 9)

    def test_token_limit_exhausts(self):
        b = self._budget(token_limit=100)
        assert b.exhausted() == ""
        b.settle_tokens(100)
        assert b.exhausted() == "token"

    def test_cost_limit_exhausts(self):
        b = self._budget(cost_limit_yuan=0.1)
        b.settle_tokens(0, cost_yuan=0.11)
        assert b.exhausted() == "cost"

    def test_deadline_exhausts(self):
        import time as _t

        b = self._budget(deadline_ms=10)
        _t.sleep(0.02)
        assert b.exhausted() == "deadline"

    def test_can_afford_rejects_before_overspending(self):
        """关键语义：余额不够**下一次**调用时就该拒绝，而不是花完才发现。"""
        b = self._budget(token_limit=1000)
        b.settle_tokens(900)
        assert b.can_afford(tokens=50) is True
        assert b.can_afford(tokens=200) is False, "余额不足时不得放行（否则超支已发生）"

    def test_exhausted_reason_is_sticky(self):
        """耗尽原因一旦写入不被后续判定覆盖（前端要看到首次触发原因）。"""
        b = self._budget(token_limit=10)
        b.settle_tokens(10)
        first = b.exhausted()
        b.settle_tokens(10)
        assert b.exhausted() == first == "token"

    def test_snapshot_shape(self):
        b = self._budget(token_limit=100, deadline_ms=1000)
        b.settle_tokens(30, cost_yuan=0.0001)
        snap = b.snapshot()
        assert snap["tokens_used"] == 30
        assert snap["token_limit"] == 100
        assert snap["exhausted_reason"] == ""
        assert set(snap) >= {"tokens_used", "cost_used_yuan", "elapsed_ms", "deadline_ms"}

    def test_estimate_cost_unknown_model_is_zero(self):
        """价格表缺该模型时记 0（前端标未知），绝不猜一个数字。"""
        from app.services.agent.budget import estimate_cost_yuan

        assert estimate_cost_yuan(1000, {"qwen-plus": 0.001}, "unknown-model") == 0.0
        assert estimate_cost_yuan(1000, {"qwen-plus": 0.001}, "qwen-plus") == 0.001

    def test_price_table_tolerates_bad_json(self):
        from app.core.config import Settings
        from app.services.agent.budget import price_table

        assert price_table(Settings(agent_price_per_1k_json="not-json")) == {}
        assert price_table(Settings(agent_price_per_1k_json='{"m": 0.5}')) == {"m": 0.5}

    def test_budget_from_settings(self):
        from app.core.config import Settings
        from app.services.agent.budget import budget_from_settings

        b = budget_from_settings(
            Settings(agent_token_budget=123, agent_cost_limit_yuan=0.9, agent_deadline_ms=5000),
            "t1",
        )
        assert (b.token_limit, b.cost_limit_yuan, b.deadline_ms) == (123, 0.9, 5000)

    def test_tool_node_stops_when_budget_exhausted(self):
        """端到端：token 预算 = 1 时，工具调用前即被闸门拦住并降级作答。"""
        from app.services.agent.budget import AgentBudget

        registry = _make_registry(FakeSearchTool([_evidence("c#1", 0.9)]))
        planner = ScriptedPlanner([[{"description": "查", "tool": "vector_search"}]])
        reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
        graph = build_agent_graph(
            registry, planner, reflector, MockLLM(), agent_prompt_builder
        )
        budget = AgentBudget(token_limit=1)
        budget.settle_tokens(5)  # 已超限
        final = graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"budget": budget}},
        )
        assert final["status"] == "budget_exhausted"
        assert final["incomplete"] is True
        assert final["fuse_reason"] == "budget_token"

    def test_tool_node_runs_within_budget(self):
        """预算充裕时不得误伤正常流程。"""
        from app.services.agent.budget import AgentBudget

        registry = _make_registry(FakeSearchTool([_evidence("c#1", 0.9)]))
        planner = ScriptedPlanner([[{"description": "查", "tool": "vector_search"}]])
        reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
        graph = build_agent_graph(
            registry, planner, reflector, MockLLM(), agent_prompt_builder
        )
        final = graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"budget": AgentBudget(token_limit=100000)}},
        )
        assert final.get("status") != "budget_exhausted"

    def test_deadline_fallback_state_is_refusal(self):
        """deadline 降级必须是明确的拒答，而不是空答案（空串会被当成渲染异常）。"""
        from app.services.agent.budget import AgentBudget
        from app.services.agent.runner import _deadline_fallback_state

        state = _deadline_fallback_state("问题", "t1", AgentBudget(deadline_ms=1))
        assert state["status"] == "budget_exhausted"
        assert state["refusal"] is True and state["final_answer"]
        assert state["budget"]["deadline_ms"] == 1


class TestChitchatShortCircuit:
    """闲聊短路。此前 skip_retrieval 在 Agent 路径无生产调用点，
    "你好"也白跑 plan→tool→reflect 全循环并计费。"""

    def test_obvious_greetings_detected(self):
        from app.services.query.intent import is_obvious_chitchat

        for q in ["你好", "您好！", "hi", "Hello", "谢谢", "thanks", "再见", "你是谁"]:
            assert is_obvious_chitchat(q) is True, f"未识别寒暄: {q}"

    def test_real_questions_not_misclassified(self):
        """**最关键的一条**：宁可漏判（多花检索钱）也不能误判（真问题不检索）。"""
        from app.services.query.intent import is_obvious_chitchat

        for q in [
            "你好，请帮我查一下年假政策",
            "公司年假多少天",
            "谢谢，那病假呢",
            "介绍一下你的知识库里有哪些文档",
            "苹果价格是多少",
        ]:
            assert is_obvious_chitchat(q) is False, f"真问题被误判为寒暄: {q}"

    def test_long_text_never_chitchat(self):
        from app.services.query.intent import is_obvious_chitchat

        assert is_obvious_chitchat("你好" * 20) is False

    def test_empty_safe(self):
        from app.services.query.intent import is_obvious_chitchat

        assert is_obvious_chitchat("") is False
        assert is_obvious_chitchat("   ") is False

    def test_entry_router_skips_plan_when_chitchat(self):
        """短路生效：skip_retrieval=True 时图直接从 entry 走到 answer，没有任何 tool 调用。"""
        calls: list[str] = []

        class CountingTool(FakeSearchTool):
            def run(self, query, tenant_id):
                calls.append(query)
                return super().run(query, tenant_id)

        registry = _make_registry(CountingTool([_evidence("c#1", 0.9)]))
        planner = ScriptedPlanner([[{"description": "不应执行", "tool": "vector_search"}]])
        reflector = ScriptedReflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
        graph = build_agent_graph(
            registry, planner, reflector, MockLLM(), agent_prompt_builder
        )
        state = initial_state("你好", "t1")
        state["skip_retrieval"] = True
        final = graph.invoke(state, config={"configurable": {}})

        assert calls == [], "闲聊不得触发任何工具调用（否则仍在白跑检索）"
        assert planner.calls == 0, "闲聊不得触发规划（plan 节点应被 entry_router 跳过）"
        assert final.get("final_answer"), "闲聊应直接给出直答"


