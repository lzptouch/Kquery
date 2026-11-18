"""统计显著性（`EV-04`）：**配对 bootstrap**。

## 为什么用配对 bootstrap

LLM 评分分布**不严格服从正态分布**，两组独立样本做 t 检验不稳健；
而**配对差值**（同一批查询下两方案的逐条差）消掉了「题目难度差异」，方差更小。

对配对差值做 bootstrap 重采样构造置信区间：方差更稳定、区间比
「两组区间相减」更可靠、且**无需分布假设**。

## 为什么不引入 scipy

`services/evaluation/` 目前是**纯函数、零第三方依赖**的模块（numpy 只被
`chunkers/raptor.py` 消费）。scipy 会给后端运行时引入一个重依赖；
bootstrap 的统计量（配对差值均值）用纯 Python 十几行即可，
且**可复现**（固定 seed）。

## 口径纪律（硬约束）

1. **样本量 < 30 → 标「样本不足」且不判显著**——
   不允许拿 20 条样本下的显著性当结论；
2. **区间缺失时字段为 `None`**（不留 0 冒充）；
3. 显著性**只用于比较同一批查询上的两个方案**，不用于跨语料比较。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

#: 样本量下限（低于此值不判显著）
MIN_SAMPLE_SIZE = 30
#: 默认重采样次数
DEFAULT_RESAMPLES = 1000
#: 默认置信水平
DEFAULT_CONFIDENCE = 0.95


@dataclass(frozen=True)
class SignificanceResult:
    """一次显著性检验的结果。"""

    mean_diff: float
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    significant: bool
    sample_size: int
    insufficient: bool
    resamples: int
    confidence: float
    note: str = ""


def paired_bootstrap(
    a: list[float],
    b: list[float],
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 20260101,
    confidence: float = DEFAULT_CONFIDENCE,
    min_sample: int = MIN_SAMPLE_SIZE,
) -> SignificanceResult:
    """对**配对差值**做 bootstrap 置信区间与双侧 p 值。

    - `a` / `b`：两个方案在**同一批查询**上的逐条得分（长度必须相同）；
    - 差值方向：`a - b`（正数 = a 更好）；
    - `seed` 固定 → 结果**完全可复现**；
    - 样本量不足 → 返回 `significant=False` + `insufficient=True` + `note`，
      但**仍然给出点估计与区间**（区间可用于观察，但不用于下结论）。
    """
    if len(a) != len(b):
        raise ValueError(f"配对 bootstrap 要求长度相等：{len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        raise ValueError("配对 bootstrap 至少要 1 对样本")

    diffs = [float(x) - float(y) for x, y in zip(a, b)]
    mean_diff = sum(diffs) / n

    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(max(1, int(resamples))):
        # **按查询配对**重采样：抽的是「第 i 条查询」，两方案同一条一起换
        sample = [diffs[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()

    alpha = (1.0 - confidence) / 2.0
    lo_idx = max(0, min(len(means) - 1, int(alpha * len(means))))
    hi_idx = max(0, min(len(means) - 1, int((1.0 - alpha) * len(means)) - 1))
    ci_low = means[lo_idx]
    ci_high = means[hi_idx]

    # 双侧 p 值：观测到的均值与零假设（均值=0）的偏离，在重采样分布里的占比
    if mean_diff >= 0:
        extreme = sum(1 for m in means if m <= 0.0)
    else:
        extreme = sum(1 for m in means if m >= 0.0)
    p_value = min(1.0, 2.0 * extreme / len(means))

    insufficient = n < min_sample
    significant = (not insufficient) and (ci_low > 0.0 or ci_high < 0.0)
    note = ""
    if insufficient:
        note = (
            f"样本量 {n} < {min_sample}：不判显著"
            "（不允许拿小样本下的显著性当结论）"
        )
    elif not significant:
        note = "置信区间包含 0：差异与噪声无法区分"
    return SignificanceResult(
        mean_diff=round(mean_diff, 6),
        ci_low=round(ci_low, 6),
        ci_high=round(ci_high, 6),
        p_value=round(p_value, 6),
        significant=significant,
        sample_size=n,
        insufficient=insufficient,
        resamples=len(means),
        confidence=confidence,
        note=note,
    )


def as_dict(result: SignificanceResult) -> dict:
    """报告用的字典形态（缺失值为 `None`，**不留 0 冒充**）。"""
    return {
        "mean_diff": result.mean_diff,
        "ci_low": result.ci_low,
        "ci_high": result.ci_high,
        "p_value": result.p_value,
        "significant": result.significant,
        "sample_size": result.sample_size,
        "insufficient": result.insufficient,
        "resamples": result.resamples,
        "confidence": result.confidence,
        "note": result.note,
    }
