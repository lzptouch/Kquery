"""ReAct 单步决策循环。

要证明的核心事实：**控制流由 LLM 决定**，而不是照着开工前的清单执行。
改造前 tool 节点是纯 if-else（选工具/拼入参/判成功全硬编码），
LLM 只在 plan（开工前）与 reflect（事后打分）出现。
"""
import pytest

from app.services.agent.react import (
    Observation,
    ReActThinker,
    observations_from_tool_results,
)


class _ScriptedLLM:
    """按脚本返回 JSON（模拟 LLM 的决策输出）。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        idx = min(self.calls - 1, len(self._script) - 1)
        return self._script[idx]

    def stream(self, prompt: str):
        yield self.generate(prompt)


CATALOG = [
    {"name": "vector_search", "description": "检索文档",
     "parameters": {"properties": {"query": {}, "doc_id": {}}}, "danger": "read_only"},
    {"name": "calculator", "description": "计算",
     "parameters": {"properties": {"query": {}}}, "danger": "safe"},
]


class TestThinkParsing:
    def test_parses_structured_decision(self):
        llm = _ScriptedLLM(['{"thought":"需要先查文档","tool_calls":[{"name":"vector_search",'
                            '"arguments":{"query":"年假政策"},"purpose":"找依据"}],'
                            '"done":false,"answer_hint":""}'])
        out = ReActThinker(llm).think("年假多少天", [], CATALOG)
        assert out.degraded is False
        assert out.decision.thought == "需要先查文档"
        assert out.decision.tool_calls[0].name == "vector_search"
        assert out.decision.tool_calls[0].arguments == {"query": "年假政策"}

    def test_parses_done_decision(self):
        llm = _ScriptedLLM(['{"thought":"够了","tool_calls":[],"done":true,"answer_hint":"用[#1]"}'])
        out = ReActThinker(llm).think(
            "q", [Observation(tool="vector_search", arguments={}, summary="年假5天", hit_count=1)],
            CATALOG, evidence_count=1,
        )
        assert out.decision.done is True
        assert out.decision.tool_calls == []

    def test_limits_tool_calls_per_turn(self):
        """单轮工具数有上限——否则模型可能一次要求十个调用，拖垮延迟。"""
        calls = ",".join(
            f'{{"name":"vector_search","arguments":{{"query":"q{i}"}},"purpose":"p"}}'
            for i in range(10)
        )
        llm = _ScriptedLLM([f'{{"thought":"t","tool_calls":[{calls}],"done":false}}'])
        out = ReActThinker(llm).think("q", [], CATALOG)
        assert len(out.decision.tool_calls) <= 3

    def test_drops_blank_tool_names(self):
        llm = _ScriptedLLM(['{"thought":"t","tool_calls":[{"name":"","arguments":{}},'
                            '{"name":"calculator","arguments":{"query":"1+1"}}],"done":false}'])
        out = ReActThinker(llm).think("q", [], CATALOG)
        assert [c.name for c in out.decision.tool_calls] == ["calculator"]


class TestDegradation:
    """解析失败必须降级而非抛异常——否则整个问答直接失败。"""

    @pytest.mark.parametrize("bad", [
        "这不是 JSON",
        '{"thought": 123}',            # 类型不符
        "{{{ 完全不是 JSON",            # 括号都不配对（jsonutil 的容错也救不了）
        "",                            # 空响应
    ])
    def test_bad_output_degrades_not_raises(self, bad):
        out = ReActThinker(_ScriptedLLM([bad])).think("q", [], CATALOG)
        assert out.degraded is True
        assert out.degraded_reason.startswith("react_think_parse")

    def test_retries_then_succeeds(self):
        llm = _ScriptedLLM([
            "坏的",
            '{"thought":"修好了","tool_calls":[{"name":"calculator",'
            '"arguments":{"query":"1+1"},"purpose":"p"}],"done":false}',
        ])
        out = ReActThinker(llm, max_retries=1).think("q", [], CATALOG)
        assert out.degraded is False and llm.calls == 2

    def test_exception_degrades(self):
        class Boom:
            def generate(self, prompt):
                raise RuntimeError("上游炸了")

        out = ReActThinker(Boom()).think("q", [], CATALOG)
        assert out.degraded is True


class TestHallucinationGuard:
    def test_done_without_evidence_is_ignored(self):
        """防幻觉式跳过检索：自称完成但毫无证据 → 强制至少一轮工具调用。"""
        llm = _ScriptedLLM(['{"thought":"我知道答案","tool_calls":[],"done":true,"answer_hint":"凭记忆"}'])
        out = ReActThinker(llm).think("q", [], CATALOG, evidence_count=0)
        assert out.decision.done is False, "无证据时不得直接作答"
        assert out.decision.tool_calls, "应被强制生成一个检索意图"

    def test_done_with_evidence_is_honoured(self):
        llm = _ScriptedLLM(['{"thought":"够了","tool_calls":[],"done":true}'])
        out = ReActThinker(llm).think(
            "q", [Observation(tool="vector_search", arguments={}, summary="s", hit_count=1)],
            CATALOG, evidence_count=1,
        )
        assert out.decision.done is True


class TestPromptConstruction:
    def test_prompt_includes_tool_schema(self):
        """prompt 必须带**参数名**——否则模型不知道能传 doc_id/top_k。"""
        captured = {}

        class Capture:
            def generate(self, prompt):
                captured["p"] = prompt
                return '{"thought":"t","tool_calls":[],"done":false}'

        ReActThinker(Capture()).think("问题", [], CATALOG)
        assert "doc_id" in captured["p"], "工具 schema 未进 prompt"
        assert "vector_search" in captured["p"]
        assert "问题" in captured["p"]

    def test_prompt_includes_observations(self):
        captured = {}

        class Capture:
            def generate(self, prompt):
                captured["p"] = prompt
                return '{"thought":"t","tool_calls":[],"done":false}'

        obs = [Observation(tool="vector_search", arguments={"query": "年假"},
                           summary="年假为 5 天", hit_count=2)]
        ReActThinker(Capture()).think("问题", obs, CATALOG)
        assert "年假为 5 天" in captured["p"], "历史观察未进 prompt（无法基于结果决策）"

    def test_prompt_warns_against_repeat(self):
        captured = {}

        class Capture:
            def generate(self, prompt):
                captured["p"] = prompt
                return '{"thought":"t","tool_calls":[],"done":false}'

        ReActThinker(Capture()).think("q", [], CATALOG)
        assert "不要重复" in captured["p"], "应显式抑制重复调用"


class TestObservations:
    def test_success_observation(self):
        obs = observations_from_tool_results([{
            "tool_name": "vector_search", "arguments": {"query": "q"},
            "items": [{"content": "内容A"}, {"content": "内容B"}],
        }])
        assert obs[0].hit_count == 2 and not obs[0].error

    def test_error_observation(self):
        obs = observations_from_tool_results([{
            "tool_name": "sql_search", "arguments": {}, "items": [],
            "error": "参数不合法（top_k 超限）",
        }])
        assert "参数不合法" in obs[0].error
        assert "失败" in obs[0].to_prompt_line()

    def test_empty_result_marked(self):
        obs = observations_from_tool_results([{
            "tool_name": "vector_search", "arguments": {}, "items": [],
        }])
        assert "无结果" in obs[0].to_prompt_line()

    def test_summary_is_truncated(self):
        """观察必须截断——它会累积进每一轮 prompt，全文塞入会让成本随轮数平方增长。"""
        long_text = "很长的内容" * 200
        obs = observations_from_tool_results([{
            "tool_name": "vector_search", "arguments": {},
            "items": [{"content": long_text}],
        }], summary_chars=80)
        assert len(obs[0].summary) <= 80


class TestModeSelection:
    """agent_mode 决定走哪条路径；默认仍是 plan_execute（可验证性优先）。"""

    def test_default_mode_is_plan_execute(self):
        from app.core.config import Settings

        assert Settings().agent_mode == "plan_execute"

    def test_runner_creates_thinker_only_in_react_mode(self):
        from app.core.config import Settings
        from app.services.agent.runner import AgentRunner
        from app.services.agent.tools import ToolRegistry
        from app.services.llm import MockLLM

        reg = ToolRegistry(timeout_s=2)
        graph = type("G", (), {"invoke": lambda *a, **k: {}})()

        plan = AgentRunner(graph, None, llm=MockLLM(), settings=Settings(), registry=reg)
        assert plan._thinker is None, "默认模式不应启用 ReAct"

        react = AgentRunner(
            graph, None, llm=MockLLM(),
            settings=Settings(agent_mode="react"), registry=reg,
        )
        assert react._thinker is not None, "react 模式应启用 thinker"

    def test_graph_routes_to_think_when_thinker_injected(self):
        """入口路由：注入 thinker → think 节点；否则 → plan 节点。"""
        from app.services.agent.graph import build_agent_graph
        from app.services.agent.planner import HeuristicPlanner
        from app.services.agent.reflector import HeuristicReflector
        from app.services.agent.state import initial_state
        from app.services.agent.tools import CalculatorTool, ToolRegistry
        from app.services.llm import MockLLM

        from tests.integration.test_stage6_agent import FakeSearchTool, agent_prompt_builder

        reg = ToolRegistry(timeout_s=2)
        reg.register(FakeSearchTool([{"chunk_id": "c#1", "content": "x", "score": 0.9,
                                      "source": "s", "doc_id": "d"}]))
        reg.register(CalculatorTool())
        graph = build_agent_graph(
            reg, HeuristicPlanner(reg), HeuristicReflector(), MockLLM(), agent_prompt_builder
        )

        # 不注入 thinker → 走 plan（行为与改造前一致）
        state = initial_state("年假 policy", "t1")
        with_plan = graph.invoke(state, config={"configurable": {}})
        assert "think" not in str(with_plan.get("next_action", ""))


class TestEndToEndReAct:
    def _graph(self, script):
        from app.services.agent.graph import build_agent_graph
        from app.services.agent.planner import HeuristicPlanner
        from app.services.agent.reflector import HeuristicReflector
        from app.services.agent.tools import CalculatorTool, ToolRegistry
        from app.services.llm import MockLLM

        from tests.integration.test_stage6_agent import FakeSearchTool, agent_prompt_builder

        reg = ToolRegistry(timeout_s=2)
        reg.register(FakeSearchTool([{"chunk_id": "c#1", "content": "年假为 5 天",
                                      "score": 0.9, "source": "s", "doc_id": "d"}]))
        reg.register(CalculatorTool())
        graph = build_agent_graph(
            reg, HeuristicPlanner(reg), HeuristicReflector(), MockLLM(), agent_prompt_builder
        )
        return graph, reg, ReActThinker(_ScriptedLLM(script))

    def test_react_loop_runs_tool_then_answers(self):
        """完整一轮：think 要求检索 → 执行 → reflect → think 判定完成 → answer。"""
        script = [
            '{"thought":"先查文档","tool_calls":[{"name":"vector_search",'
            '"arguments":{"query":"年假"},"purpose":"找依据"}],"done":false}',
            '{"thought":"已足够","tool_calls":[],"done":true,"answer_hint":"用证据作答"}',
        ]
        graph, reg, thinker = self._graph(script)
        from app.services.agent.state import initial_state

        final = graph.invoke(
            initial_state("年假多少天", "t1"),
            config={"configurable": {
                "thinker": thinker,
                "tool_catalog": reg.catalog(),
                "acl_filter": None,
            }},
        )
        calls = final.get("tool_calls") or []
        assert calls, "ReAct 循环应实际执行了工具（LLM 的决策被落实）"
        assert calls[0]["tool_name"] == "vector_search"
        assert final.get("final_answer")

    def test_react_emits_thought_events(self):
        """轨迹必须可解释：每步的 thought 要发出去（否则用户看不懂为什么这么查）。"""
        script = [
            '{"thought":"我先确认定义","tool_calls":[{"name":"vector_search",'
            '"arguments":{"query":"定义"},"purpose":"p"}],"done":false}',
            '{"thought":"完成","tool_calls":[],"done":true}',
        ]
        graph, reg, thinker = self._graph(script)
        from app.services.agent.state import initial_state

        events: list = []
        graph.invoke(
            initial_state("q", "t1"),
            config={"configurable": {
                "thinker": thinker, "tool_catalog": reg.catalog(),
                "emit": lambda e, p: events.append((e, p)),
            }},
        )
        thoughts = [p for e, p in events if e == "thought"]
        assert thoughts, "未发出 thought 事件（轨迹不可解释）"
        assert thoughts[0]["thought"] == "我先确认定义"

    def test_degraded_thinker_falls_back_to_plan(self):
        """think 解析失败 → 降级 plan_execute，而不是整个问答失败。"""
        graph, reg, thinker = self._graph(["完全不是 JSON", "还是不是 JSON", "依旧不是"])
        from app.services.agent.state import initial_state

        final = graph.invoke(
            initial_state("年假多少天", "t1"),
            config={"configurable": {
                "thinker": thinker, "tool_catalog": reg.catalog(),
            }},
        )
        assert final.get("react_degraded"), "应记录降级原因"
        assert final.get("final_answer") or final.get("refusal") is not None, (
            "降级后仍必须给出产出（不能因 ReAct 失败就无应答）"
        )
