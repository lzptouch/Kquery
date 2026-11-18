"""生成质量 rubric（`EV-02`）。

## 四条验收

1. 权重和为 1
2. 加权分计算正确
3. 关键维度低于下限 → 整体不通过
4. rubric 文件缺维度 / 权重和不为 1 → **抛错**（不静默）
"""
from __future__ import annotations

import json

import pytest

from app.services.evaluation.rubric import (
    DEFAULT_RUBRIC_PATH,
    RubricError,
    available_rubrics,
    load_rubric,
)


def test_repository_rubric_weights_sum_to_one():
    """① 仓库内的 rubric 权重和为 1。"""
    rubric = load_rubric()
    assert abs(sum(d.weight for d in rubric.dims) - 1.0) < 1e-6
    assert rubric.version, "版本号必填（没有版本号的评分无法比较）"


def test_repository_rubric_has_five_dimensions():
    """仓库内的 rubric 是五维（事实性优先的权重分配）。"""
    rubric = load_rubric()
    ids = [d.id for d in rubric.dims]
    assert ids == [
        "answer_correctness",
        "citation_faithfulness",
        "completeness",
        "conciseness",
        "refusal_correctness",
    ]
    # 正确性的权重必须最高
    weights = {d.id: d.weight for d in rubric.dims}
    assert weights["answer_correctness"] == max(weights.values())


def test_weighted_score_is_correct():
    """② 加权分计算正确。"""
    rubric = load_rubric()
    scores = {
        "answer_correctness": 1.0,
        "citation_faithfulness": 0.5,
        "completeness": 0.0,
        "conciseness": 1.0,
        "refusal_correctness": 0.5,
    }
    expected = (
        0.40 * 1.0 + 0.25 * 0.5 + 0.15 * 0.0 + 0.10 * 1.0 + 0.10 * 0.5
    )
    assert rubric.weighted_score(scores) == pytest.approx(expected, abs=1e-6)


def test_critical_dim_below_threshold_fails():
    """③ 关键维度低于下限 → 整体不通过（即使加权分够高）。"""
    rubric = load_rubric()
    scores = {d.id: 1.0 for d in rubric.dims}
    scores["citation_faithfulness"] = 0.5  # 低于关键下限 0.8
    passed, reason = rubric.passes(scores)
    assert passed is False
    assert "关键维度" in reason


def test_missing_score_is_an_error_not_zero():
    """缺维度分数 → **抛错**（不允许静默当 0 分）。"""
    rubric = load_rubric()
    with pytest.raises(RubricError):
        rubric.weighted_score({"answer_correctness": 1.0})


def test_out_of_range_score_is_an_error():
    """分数越界 → 抛错（不静默截断）。"""
    rubric = load_rubric()
    scores = {d.id: 1.0 for d in rubric.dims}
    scores["conciseness"] = 1.5
    with pytest.raises(RubricError):
        rubric.weighted_score(scores)


def _write(tmp_path, payload: dict):
    p = tmp_path / "rubric.bad.json"
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return p


def test_weights_not_summing_to_one_is_rejected(tmp_path):
    """④ 权重和不为 1 → 抛错（加载即校验）。"""
    payload = json.loads(DEFAULT_RUBRIC_PATH.read_text(encoding="utf-8"))
    payload["dims"][0]["weight"] = 0.9
    p = _write(tmp_path, payload)
    with pytest.raises(RubricError) as excinfo:
        load_rubric(p)
    assert "权重和" in str(excinfo.value)


def test_dimension_missing_field_is_rejected(tmp_path):
    """④ 维度条目缺字段 → 抛错（不允许「缺维度 = 0 分」这种静默降级）。

    注意：**减少维度本身是合法的**（rubric 自己定义维度集合，
    judge 侧会校验「返回的维度集合 == rubric 维度集合」），
    非法的是「维度条目缺字段」。
    """
    payload = json.loads(DEFAULT_RUBRIC_PATH.read_text(encoding="utf-8"))
    payload["dims"][0].pop("criteria")
    p = _write(tmp_path, payload)
    with pytest.raises(RubricError) as excinfo:
        load_rubric(p)
    assert "criteria" in str(excinfo.value)


def test_dimension_without_scale_is_rejected(tmp_path):
    """维度缺 scale → 抛错。"""
    payload = json.loads(DEFAULT_RUBRIC_PATH.read_text(encoding="utf-8"))
    payload["dims"][0].pop("scale")
    p = _write(tmp_path, payload)
    with pytest.raises(RubricError):
        load_rubric(p)


def test_missing_version_is_rejected(tmp_path):
    """缺版本号 → 抛错。"""
    payload = json.loads(DEFAULT_RUBRIC_PATH.read_text(encoding="utf-8"))
    payload.pop("version")
    p = _write(tmp_path, payload)
    with pytest.raises(RubricError) as excinfo:
        load_rubric(p)
    assert "version" in str(excinfo.value)


def test_critical_dim_must_be_subset(tmp_path):
    """关键维度不是 dims 的子集 → 抛错。"""
    payload = json.loads(DEFAULT_RUBRIC_PATH.read_text(encoding="utf-8"))
    payload["pass"]["critical_dims"] = ["not_a_dim"]
    p = _write(tmp_path, payload)
    with pytest.raises(RubricError) as excinfo:
        load_rubric(p)
    assert "子集" in str(excinfo.value)


def test_missing_file_lists_available_versions():
    """找不到文件 → 报错并列出可用版本。"""
    with pytest.raises(RubricError) as excinfo:
        load_rubric("/nonexistent/rubric.v9.json")
    assert "可用版本" in str(excinfo.value)
    assert "rubric.v1.json" in available_rubrics()
