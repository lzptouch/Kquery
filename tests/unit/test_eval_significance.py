"""统计显著性（`EV-04`）：配对 bootstrap。

## 五条验收

1. 同一分布 → 区间含 0、p > 0.05（不判显著）
2. 明显差异 → 区间不含 0（判显著）
3. 同 seed 两次结果**完全一致**（可复现）
4. 样本量 < 30 → **不判显著**且标样本不足
5. 长度不等的入参 → 抛错（配对语义）
"""
from __future__ import annotations

import random

import pytest

from app.services.evaluation.significance import (
    MIN_SAMPLE_SIZE,
    as_dict,
    paired_bootstrap,
)


def _paired_sample(n: int = 60, seed: int = 7, noise: float = 0.02):
    """同一批题目上的两个方案（配对：差异只是噪声）。"""
    rng = random.Random(seed)
    a = [rng.gauss(0.5, 0.1) for _ in range(n)]
    b = [x + rng.gauss(0, noise) for x in a]
    return a, b


def test_same_distribution_is_not_significant():
    """① 同一分布 → 区间含 0、p > 0.05。"""
    a, b = _paired_sample()
    result = paired_bootstrap(a, b)
    assert result.significant is False
    assert result.ci_low <= 0.0 <= result.ci_high, result
    assert result.p_value > 0.05, result
    assert result.insufficient is False


def test_clear_difference_is_significant():
    """② 明显差异 → 区间不含 0。"""
    rng = random.Random(3)
    a = [rng.gauss(0.9, 0.05) for _ in range(60)]
    b = [rng.gauss(0.5, 0.05) for _ in range(60)]
    result = paired_bootstrap(a, b)
    assert result.significant is True
    assert result.ci_low > 0.0, result
    assert result.p_value < 0.05, result


def test_same_seed_is_reproducible():
    """③ 同 seed 两次结果完全一致。"""
    a, b = _paired_sample()
    first = paired_bootstrap(a, b, seed=42)
    second = paired_bootstrap(a, b, seed=42)
    assert first == second


def test_small_sample_is_not_judged_significant():
    """④ 样本量 < 30 → 不判显著且标样本不足（但仍给点估计与区间）。"""
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [0.0, 1.0, 2.0, 3.0, 4.0]
    result = paired_bootstrap(a, b)
    assert result.insufficient is True
    assert result.significant is False
    assert result.sample_size < MIN_SAMPLE_SIZE
    assert "样本不足" in result.note or "不判显著" in result.note
    # 区间仍然给出（可用于观察），但不用于下结论
    assert result.ci_low is not None and result.ci_high is not None


def test_length_mismatch_raises():
    """⑤ 长度不等 → 抛错（配对语义）。"""
    with pytest.raises(ValueError):
        paired_bootstrap([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        paired_bootstrap([], [])


def test_direction_is_a_minus_b():
    """差值方向：正数 = a 更好。"""
    result = paired_bootstrap([2.0, 3.0, 4.0] * 15, [1.0, 2.0, 3.0] * 15)
    assert result.mean_diff > 0


def test_report_dict_keeps_none_as_none():
    """报告形态：缺失值为 None（不留 0 冒充）。"""
    a, b = _paired_sample()
    payload = as_dict(paired_bootstrap(a, b))
    assert set(payload) >= {
        "mean_diff", "ci_low", "ci_high", "p_value",
        "significant", "sample_size", "insufficient",
    }
    assert isinstance(payload["mean_diff"], float)
    assert payload["sample_size"] == 60
