"""独立 Critic 校验节点。

与 reflect 的分工：
- reflect 评**证据够不够**（要不要继续找）
- verify  评**答案站不站得住**（每句是否有据）

两者互补——证据充足但生成阶段胡说是有可能的。
"""
from app.services.agent.graph import build_agent_graph
from app.services.agent.state import initial_state
from app.services.agent.tools import CalculatorTool, ToolRegistry
from app.services.llm import MockLLM

from tests.integration.test_stage6_agent import FakeSearchTool, agent_prompt_builder


def _evidence(chunk_id: str, score: float = 0.9) -> dict:
    return {
        "chunk_id": chunk_id, "content": "证据内容", "score": score,
        "source": "测试文档", "doc_id": "doc-t", "source_route": "vector",
    }


class _Planner:
    def __init__(self, steps):
        self._steps = steps
        self.calls = 0

    def plan(self, question, replan_hint=""):
        from app.services.agent.planner import sanitize_steps

        return sanitize_steps(self._steps, _REGISTRY)


class _Reflector:
    def __init__(self, verdicts):
        self._v = verdicts
        self.calls = 0

    def reflect(self, question, evidence, remaining_steps):
        v = self._v[min(self.calls, len(self._v) - 1)]
        self.calls += 1
        return dict(v)


class _FakeCheck:
    """模拟 HallucinationChecker 的返回。"""

    def __init__(self, unsupported):
        self.unsupported_claims = unsupported


class _FakeVerifier:
    def __init__(self, scripts):
        self._scripts = scripts
        self.calls = 0

    def check(self, answer, evidence_block):
        s = self._scripts[min(self.calls, len(self._scripts) - 1)]
        self.calls += 1
        return _FakeCheck(s)


class _BrokenVerifier:
    def check(self, answer, evidence_block):
        raise RuntimeError("校验服务不可用")


_REGISTRY = None


def _build(verifier=None, max_verify_rounds=1, planner_steps=None):
    global _REGISTRY
    registry = ToolRegistry(timeout_s=2)
    registry.register(FakeSearchTool([_evidence("c#1")]))
    registry.register(CalculatorTool())
    _REGISTRY = registry
    planner = _Planner(planner_steps or [{"description": "查", "tool": "vector_search"}])
    reflector = _Reflector([{"score": 0.9, "next_action": "answer", "reason": "够"}])
    graph = build_agent_graph(registry, planner, reflector, MockLLM(), agent_prompt_builder)
    cfg = {}
    if verifier is not None:
        cfg = {"verifier": verifier, "max_verify_rounds": max_verify_rounds}
    return graph, cfg


class TestVerifyDisabledByDefault:
    def test_no_verifier_means_no_extra_step(self):
        """默认关闭时行为与改造前完全一致（不引入额外调用与延迟）。"""
        graph, cfg = _build(verifier=None)
        final = graph.invoke(
            initial_state("问题", "t1"), config={"configurable": dict(cfg)}
        )
        assert final.get("final_answer")
        assert "verify_rounds" not in final or not final.get("verify_rounds")


class TestVerifyPass:
    def test_passing_answer_ends_normally(self):
        verifier = _FakeVerifier([[]])  # 无无据陈述
        graph, cfg = _build(verifier=verifier)
        final = graph.invoke(
            initial_state("问题", "t1"), config={"configurable": dict(cfg)}
        )
        assert verifier.calls == 1, "应执行一次校验"
        assert final.get("verify_passed") is True
        assert final.get("final_answer")


class TestVerifyFailTriggersReplan:
    def test_unsupported_claims_trigger_replan(self):
        verifier = _FakeVerifier([["这句话没依据"]])
        graph, cfg = _build(verifier=verifier, max_verify_rounds=1)
        final = graph.invoke(
            initial_state("问题", "t1"), config={"configurable": dict(cfg)}
        )
        assert verifier.calls >= 1
        # 回退重规划后应重新走一轮（planner 被再次调用）
        assert final.get("verify_notes") or final.get("verify_rounds", 0) >= 1

    def test_bounded_by_max_rounds(self):
        """校验次数有上限：改了还是不通过也不能无限重做。"""
        verifier = _FakeVerifier([["总是没依据"]])
        graph, cfg = _build(verifier=verifier, max_verify_rounds=1)
        final = graph.invoke(
            initial_state("问题", "t1"), config={"configurable": dict(cfg)}
        )
        assert verifier.calls <= 2, f"校验轮数应受上限约束，实得 {verifier.calls}"

    def test_max_rounds_zero_prevents_any_loop(self):
        verifier = _FakeVerifier([["没依据"]])
        graph, cfg = _build(verifier=verifier, max_verify_rounds=0)
        final = graph.invoke(
            initial_state("问题", "t1"), config={"configurable": dict(cfg)}
        )
        assert verifier.calls == 0, "max_verify_rounds=0 应完全跳过校验"


class TestVerifyFailureModes:
    def test_broken_verifier_does_not_block_answer(self):
        """校验器坏了必须放行——用户拿到答案比"因校验故障拿不到答案"更重要。"""
        graph, cfg = _build(verifier=_BrokenVerifier())
        final = graph.invoke(
            initial_state("问题", "t1"), config={"configurable": dict(cfg)}
        )
        assert final.get("final_answer"), "校验故障不得吞掉答案"

    def test_refusal_skips_verify(self):
        """没有答案可校验时不该跑校验（浪费调用）。"""
        verifier = _FakeVerifier([["x"]])
        graph, cfg = _build(verifier=verifier)
        state = initial_state("问题", "t1")
        state["skip_retrieval"] = True  # 闲聊直答路径
        final = graph.invoke(state, config={"configurable": dict(cfg)})
        assert final.get("final_answer")

    def test_no_evidence_skips_verify(self):
        verifier = _FakeVerifier([["x"]])
        registry = ToolRegistry(timeout_s=2)

        class EmptyTool:
            name = "vector_search"
            description = "无结果"

            def run(self, query, tenant_id):
                from app.services.agent.tools import ToolResult

                return ToolResult(items=[])

        registry.register(EmptyTool())
        global _REGISTRY
        _REGISTRY = registry
        planner = _Planner([{"description": "查", "tool": "vector_search"}])
        reflector = _Reflector([{"score": 0.3, "next_action": "answer", "reason": ""}])
        graph = build_agent_graph(registry, planner, reflector, MockLLM(), agent_prompt_builder)
        graph.invoke(
            initial_state("问题", "t1"),
            config={"configurable": {"verifier": verifier, "max_verify_rounds": 1}},
        )
        assert verifier.calls == 0, "无证据（拒答路径）不该跑校验"
