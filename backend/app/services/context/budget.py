"""上下文预算的**唯一真相源**（`ST-01`）。

## 为什么要有

此前预算散落在三处：`core/config.py` 的 `context_token_budget`、Agent 的三个预算、
以及装配函数里各写各的 `token_budget` 默认值。想回答「这次到底能给多少 token」
必须翻三个地方。

这里把预算收成一份：**总量从配置派生，各来源按固定比例分配，输出预留单列**。

## 分配比例

| 来源 | 比例 | 为什么是这个比例 |
|---|---|---|
| 证据 | 60% | 答案的事实来源，没有它就没有答案 |
| 工具 | 20% | 工具目录是「能不能做对事」的前提，但条目多、可裁 |
| 历史 | 10% | 追问相关，但最容易膨胀（`ST-02` 会压它） |
| 记忆 | 5% | 租户级偏好，锦上添花 |
| 系统 | 5% | 系统约束短且固定 |

**保底段**（`pinned`）不参与配额裁剪：预算再紧，系统约束与保底证据也要留。

> **口径纪律**：比例是**起点**，不是真理。标定要用真实请求的构成表
> （`/admin/metrics/context`），改比例前先看数据。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 各来源的配额比例（和为 1）
SOURCE_QUOTA_RATIOS: dict[str, float] = {
    "evidence": 0.60,
    "tools": 0.20,
    "history": 0.10,
    "memory": 0.05,
    "system": 0.05,
}

#: 输出预留的默认比例（答案本身也要 token，不能全拿去塞证据）
DEFAULT_OUTPUT_RESERVE_RATIO = 0.25


@dataclass(frozen=True)
class ContextBudget:
    """一次请求的上下文预算。"""

    total_tokens: int
    output_reserve_tokens: int
    quotas: dict[str, int] = field(default_factory=dict)

    @property
    def available_tokens(self) -> int:
        """可用于输入的 token（总量 − 输出预留）。"""
        return max(0, self.total_tokens - self.output_reserve_tokens)

    def quota_for(self, source: str) -> int:
        """某个来源的配额（未知来源 → 0，即不分配）。"""
        return self.quotas.get(source, 0)


def build_budget(
    total_tokens: int,
    *,
    output_reserve_ratio: float = DEFAULT_OUTPUT_RESERVE_RATIO,
    ratios: dict[str, float] | None = None,
) -> ContextBudget:
    """从配置派生预算（单一真相源）。

    `total_tokens` 是**上下文总量**（含输出预留）——
    配置项 `context_token_budget` 的语义不变，只是现在内部把输出预留单列出来。
    """
    total = max(0, int(total_tokens))
    reserve = int(total * max(0.0, min(1.0, output_reserve_ratio)))
    available = max(0, total - reserve)
    ratio_map = ratios or SOURCE_QUOTA_RATIOS
    quotas: dict[str, int] = {}
    assigned = 0
    # 配额取整后可能差几个 token：把差额给最大的那个来源（证据），
    # 否则「各来源配额之和 ≠ 可用 token」会让人以为预算漏了。
    for source in sorted(ratio_map, key=lambda s: (-ratio_map[s], s)):
        quota = int(available * ratio_map[source])
        quotas[source] = quota
        assigned += quota
    if quotas:
        main = max(quotas, key=lambda s: quotas[s])
        quotas[main] += available - assigned
    return ContextBudget(
        total_tokens=total, output_reserve_tokens=reserve, quotas=quotas
    )
