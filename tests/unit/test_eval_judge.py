"""LLM-as-Judge（`EV-03`）。

## 五条验收

1. 3 次投票取**中位数**
2. 一致性计算正确
3. 模型抛异常 → `error` 非空且 `weighted` 为 None（**不产生假分**）
4. 非法 JSON → 该票作废、不崩
5. 维度缺失 → 该票作废

外加：**judge 返回的维度集合与 rubric 不符 → 全部作废**（联测 `EV-02`）。
"""
from __future__ import annotations

import json

import pytest

from app.services.evaluation.judge import HeuristicJudge, ModelJudge, parse_scores
from app.services.evaluation.rubric import load_rubric


class _FakeLLM:
    """按调用顺序返回预设文本；用尽后重复最后一个。"""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0

    def generate(self, prompt, deadline=None):
        self.calls += 1
        if not self._outputs:
            return ""
        idx = min(self.calls - 1, len(self._outputs) - 1)
        out = self._outputs[idx]
        if isinstance(out, Exception):
            raise out
        return out


def _scores_payload(**overrides) -> str:
    rubric = load_rubric()
    scores = {d.id: 1.0 for d in rubric.dims}
    scores.update(overrides)
    return json.dumps({"scores": scores}, ensure_ascii=False)


def test_median_of_three_votes():
    """① 3 次投票取中位数。"""
    rubric = load_rubric()
    llm = _FakeLLM([
        _scores_payload(answer_correctness=0.0),
        _scores_payload(answer_correctness=1.0),
        _scores_payload(answer_correctness=0.5),
    ])
    result = ModelJudge(llm, votes=3).judge("问题", "答案", rubric)
    assert result.votes == 3
    assert result.error == ""
    assert result.dim_scores["answer_correctness"] == 0.5
    assert result.weighted is not None


def test_agreement_is_computed_from_spread():
    """② 一致性 = 1 −（最高 − 最低）。"""
    rubric = load_rubric()
    llm = _FakeLLM([
        _scores_payload(conciseness=0.8),
        _scores_payload(conciseness=0.8),
        _scores_payload(conciseness=0.8),
    ])
    result = ModelJudge(llm, votes=3).judge("问题", "答案", rubric)
    assert result.agreement == pytest.approx(1.0)
    assert result.low_confidence is False


def test_low_confidence_is_flagged():
    """一致性过低 → `low_confidence`（但**不产生失败**）。"""
    rubric = load_rubric()
    llm = _FakeLLM([
        _scores_payload(answer_correctness=0.0),
        _scores_payload(answer_correctness=1.0),
        _scores_payload(answer_correctness=0.0),
    ])
    result = ModelJudge(llm, votes=3).judge("问题", "答案", rubric)
    assert result.low_confidence is True
    assert result.weighted is not None, "低置信不等于没有结果"
    assert result.error == ""


def test_model_exception_yields_error_not_fake_score():
    """③ 模型抛异常 → `error` 非空、`weighted` 为 None（**不产生假分**）。"""
    rubric = load_rubric()
    llm = _FakeLLM([RuntimeError("上游挂了")])
    result = ModelJudge(llm, votes=2).judge("问题", "答案", rubric)
    assert result.error
    assert result.weighted is None
    assert result.passed is None
    assert all(v is None for v in result.dim_scores.values()), result.dim_scores


def test_invalid_json_vote_is_discarded():
    """④ 非法 JSON → 该票作废、不崩（其余票照常出结果）。"""
    rubric = load_rubric()
    llm = _FakeLLM(["这不是 JSON", _scores_payload()])
    result = ModelJudge(llm, votes=2).judge("问题", "答案", rubric)
    assert result.votes == 1
    assert result.weighted is not None


def test_missing_dimension_vote_is_discarded():
    """⑤ 维度缺失 → 该票作废。"""
    rubric = load_rubric()
    payload = json.loads(_scores_payload())
    payload["scores"].pop("completeness")
    llm = _FakeLLM([json.dumps(payload), _scores_payload()])
    result = ModelJudge(llm, votes=2).judge("问题", "答案", rubric)
    assert result.votes == 1


def test_dimension_set_mismatch_discards_all_votes():
    """judge 返回的维度集合与 rubric 不符 → 全部作废（不猜、不补 0）。"""
    rubric = load_rubric()
    llm = _FakeLLM([json.dumps({"scores": {"unknown_dim": 1.0}})])
    result = ModelJudge(llm, votes=1).judge("问题", "答案", rubric)
    assert result.votes == 0
    assert result.error
    assert result.weighted is None


def test_fenced_json_is_accepted():
    """模型加围栏也能解析（真实模型经常这么干）。"""
    rubric = load_rubric()
    llm = _FakeLLM(["```json\n" + _scores_payload() + "\n```"])
    result = ModelJudge(llm, votes=1).judge("问题", "答案", rubric)
    assert result.votes == 1
    assert result.weighted is not None


def test_parse_scores_rejects_out_of_range():
    """分数越界 → 该票作废（不静默裁剪）。"""
    rubric = load_rubric()
    scores, err = parse_scores(_scores_payload(conciseness=1.4), rubric)
    assert scores is None
    assert "超出区间" in err


def test_prompt_contains_all_rubric_dimensions():
    """提示词里的维度集合 == rubric 维度集合（不在代码里二次定义）。"""
    from app.services.evaluation.judge_prompt import render_judge_prompt

    rubric = load_rubric()
    prompt = render_judge_prompt(rubric, "问题", "答案", evidence=["证据一"], reference="标准答案")
    for d in rubric.dims:
        assert d.id in prompt, d.id
        assert d.criteria in prompt, d.criteria
    assert "证据一" in prompt
    assert "标准答案" in prompt


class TestHeuristicJudge:
    """启发式判定器（CI 用）：确定性、可复现，但**不是**模型判定。"""

    def test_correct_answer_scores_high(self):
        rubric = load_rubric()
        result = HeuristicJudge().judge(
            "报销上限是多少？",
            "报销上限为 5000 元 [#1]",
            rubric,
            reference="单笔报销上限为 5000 元。",
            key_points=["单笔报销上限为 5000 元。"],
        )
        assert result.weighted is not None
        assert result.passed is True
        assert result.dim_scores["answer_correctness"] == 1.0

    def test_wrong_answer_scores_low(self):
        rubric = load_rubric()
        result = HeuristicJudge().judge(
            "报销上限是多少？",
            "报销上限为 8000 元 [#1]",
            rubric,
            key_points=["单笔报销上限为 5000 元。"],
        )
        assert result.dim_scores["answer_correctness"] == 0.0
        assert result.passed is False

    def test_missing_citation_fails_critical_dim(self):
        """没引用 → 关键维度（引用忠实性）为 0 → 整体不通过。"""
        rubric = load_rubric()
        result = HeuristicJudge().judge(
            "报销上限是多少？",
            "报销上限为 5000 元。",
            rubric,
            key_points=["单笔报销上限为 5000 元。"],
        )
        assert result.dim_scores["citation_faithfulness"] == 0.0
        assert result.passed is False
        assert "关键维度" in result.reason

    def test_is_deterministic(self):
        """同一输入 → 同一结果（确定性）。"""
        rubric = load_rubric()
        j = HeuristicJudge()
        a = j.judge("q", "报销上限为 5000 元 [#1]", rubric, key_points=["单笔报销上限为 5000 元。"])
        b = j.judge("q", "报销上限为 5000 元 [#1]", rubric, key_points=["单笔报销上限为 5000 元。"])
        assert a.dim_scores == b.dim_scores
        assert a.weighted == b.weighted

    def test_kind_is_declared(self):
        """必须标清身份（报告里要写 `judge_kind`）。"""
        assert HeuristicJudge.kind == "heuristic"
