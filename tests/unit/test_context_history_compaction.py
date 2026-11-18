"""历史摘要压缩（`ST-02`）。

## 五条验收

1. 超阈值触发摘要
2. 摘要后 token ≤ 压缩前的 **50%**
3. 模型抛异常 → 回落截断且**不抛错**
4. 压缩结果字段在 Agent 状态中**已声明**（否则 LangGraph 静默丢弃）
5. **预算不足时不发起压缩调用**
"""
from __future__ import annotations

from app.services.context.history import (
    METHOD_LLM,
    METHOD_SKIPPED,
    METHOD_TRUNCATE,
    compact_history,
    estimate_history_tokens,
    truncate_fallback,
)


class _LLM:
    """返回固定摘要的假模型。"""

    def __init__(self, text: str = "意图：了解报销制度\n已确认事实：上限 5000 元", fail: bool = False):
        self.text = text
        self.fail = fail
        self.calls = 0

    def generate(self, prompt, max_tokens=None, deadline=None):
        self.calls += 1
        if self.fail:
            raise RuntimeError("上游挂了")
        return self.text


def _turns(n: int, size: int = 200) -> list[dict]:
    return [
        {"question": f"问题{i} " + "问" * size, "answer": f"回答{i} " + "答" * size}
        for i in range(n)
    ]


def test_over_threshold_triggers_summary():
    """① 超阈值触发摘要。"""
    turns = _turns(6)
    llm = _LLM()
    result = compact_history(turns, llm, keep_recent=2, trigger_tokens=100)
    assert result.method == METHOD_LLM
    assert result.compacted_turns == 4
    assert llm.calls == 1
    assert "意图" in result.summary


def test_under_threshold_is_skipped():
    """低于阈值不压缩（压缩本身要花钱）。"""
    llm = _LLM()
    result = compact_history(_turns(2, size=10), llm, trigger_tokens=100000)
    assert result.method == METHOD_SKIPPED
    assert llm.calls == 0, "不该为省 token 先花一次调用"


def test_summary_halves_tokens():
    """② 摘要后 token ≤ 压缩前的 50%。"""
    turns = _turns(8)
    before = estimate_history_tokens(turns)
    result = compact_history(turns, _LLM(), keep_recent=2, trigger_tokens=100)
    assert result.tokens_before == before
    assert result.tokens_after <= before * 0.5, result.as_dict()
    assert result.ratio <= 0.5


def test_model_failure_falls_back_without_raising():
    """③ 模型抛异常 → 回落截断且不抛错。"""
    turns = _turns(6)
    result = compact_history(turns, _LLM(fail=True), keep_recent=2, trigger_tokens=100)
    assert result.method == METHOD_TRUNCATE
    assert result.compacted_turns == 4
    assert result.recent_turns, "仍要保留最近轮次"


def test_budget_short_skips_model_call():
    """⑤ 预算不足 → **不发起压缩调用**（直接截断回落）。"""
    llm = _LLM()
    result = compact_history(
        _turns(6), llm, keep_recent=2, trigger_tokens=100, budget_ok=False
    )
    assert llm.calls == 0, "预算不足时不该消耗模型调用"
    assert result.method == METHOD_TRUNCATE


def test_empty_summary_falls_back():
    """空摘要 → 回落截断（比带着空摘要继续走更诚实）。"""
    result = compact_history(_turns(6), _LLM(text=""), keep_recent=2, trigger_tokens=100)
    assert result.method == METHOD_TRUNCATE


def test_truncate_fallback_keeps_first_intent():
    """截断回落保留**首轮意图**（用户的原始问题决定整段对话的目标）。"""
    turns = _turns(5)
    result = truncate_fallback(turns, keep_recent=2)
    assert result.method == METHOD_TRUNCATE
    assert "问题0" in result.summary
    assert len(result.recent_turns) == 2


def test_agent_state_declares_compaction_fields():
    """④ 压缩结果字段在 Agent 状态中已声明。"""
    from app.services.agent.state import AgentState, initial_state

    for field in ("history_summary", "history_recent", "history_compaction"):
        assert field in AgentState.__annotations__, f"{field} 未在 AgentState 中声明"
    state = initial_state("问题", "tenant")
    assert state["history_summary"] == ""
    assert state["history_recent"] == []
    assert state["history_compaction"] == {}


def test_compaction_result_serializable():
    """结果可序列化（要进 SSE / 快照）。"""
    import json

    result = compact_history(_turns(6), _LLM(), keep_recent=2, trigger_tokens=100)
    payload = json.dumps(result.as_dict(), ensure_ascii=False)
    assert "method" in payload
    assert "tokens_before" in payload
