"""输出侧 token 预算（`ST-04`）。

## 四条验收

1. 上游调用**收到**输出上限
2. 预算耗尽 → **不发起调用**
3. **不传**时参数不出现在调用参数里（保持与上游默认一致）
4. mock provider 正常返回（`MockLLM` 的同名形参存在但忽略其值）
"""
from __future__ import annotations

from app.services.agent.budget import AgentBudget
from app.services.llm import LLMError, MockLLM


class _RecordingLLM:
    """记录上游收到的调用参数。"""

    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self._fail = fail

    def generate(self, prompt, deadline=None, max_tokens=None):
        self.calls.append({"prompt": prompt, "deadline": deadline, "max_tokens": max_tokens})
        if self._fail:
            raise LLMError(4002, "boom")
        return "答案"

    def stream(self, prompt, deadline=None, max_tokens=None):
        self.calls.append({"prompt": prompt, "deadline": deadline, "max_tokens": max_tokens})
        yield "答案"


def test_budget_computes_max_output_tokens():
    """预算 → 本次允许的最大输出 token（剩余 − 预留）。"""
    b = AgentBudget(token_limit=1000)
    b.tokens_used = 300
    assert b.max_output_tokens(reserve=100) == 600


def test_budget_exhausted_returns_zero():
    """预算耗尽 → 上限 0（调用方不应发起调用）。"""
    b = AgentBudget(token_limit=1000)
    b.tokens_used = 950
    assert b.max_output_tokens(reserve=100) == 0


def test_no_budget_uses_fallback():
    """没有 token 预算 → 用配置兜底值。"""
    b = AgentBudget()
    assert b.max_output_tokens(fallback=2000) == 2000


def test_pipeline_passes_max_tokens_to_llm():
    """① 管线把上限传给模型。"""
    from app.services.pipeline import RagPipeline

    pipeline = object.__new__(RagPipeline)
    llm = _RecordingLLM()
    pipeline._llm = llm
    pipeline._llm_generate("问题", max_tokens=500)
    assert llm.calls[-1]["max_tokens"] == 500


def test_no_max_tokens_means_no_limit():
    """③ 不传 → 不下发该参数（与上游默认一致）。"""
    from app.services.pipeline import RagPipeline

    pipeline = object.__new__(RagPipeline)
    llm = _RecordingLLM()
    pipeline._llm = llm
    pipeline._llm_generate("问题")
    assert llm.calls[-1]["max_tokens"] is None


def test_adapter_without_max_tokens_still_works():
    """适配器不支持 `max_tokens` → 退回旧调用面（不抛 TypeError）。"""
    from app.services.pipeline import RagPipeline

    class _OldLLM:
        def generate(self, prompt, deadline=None):
            return "旧适配器的答案"

    pipeline = object.__new__(RagPipeline)
    pipeline._llm = _OldLLM()
    assert pipeline._llm_generate("问题", max_tokens=500) == "旧适配器的答案"


def test_mock_llm_accepts_and_ignores_max_tokens():
    """④ mock 正常返回（形参存在但忽略其值）。"""
    llm = MockLLM()
    prompt = "用户问题：报销上限是多少？\n证据\n[#1]（来源：a.md）\n单笔报销上限为 5000 元。"
    text = llm.generate(prompt, max_tokens=10)
    assert "报销上限" in text
    pieces = list(llm.stream(prompt, max_tokens=10))
    assert pieces


def test_router_forwards_max_tokens():
    """路由器也要透传上限（它是 LLM 协议的实现之一）。"""
    from app.services.llm_router import LLMRouter, ModelPolicy

    llm = _RecordingLLM()
    router = LLMRouter(lambda name: llm, ModelPolicy("m1"))
    router.generate("问题", max_tokens=321)
    assert llm.calls[-1]["max_tokens"] == 321
