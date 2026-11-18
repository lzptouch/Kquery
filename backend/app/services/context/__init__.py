"""上下文包（`ST-01`）。

## 结构

| 模块 | 职责 |
|---|---|
| `assembly.py` | 证据装配（原有的 `assemble_context`：预算内选取 + MMR 多样性） |
| `budget.py` | **预算的唯一真相源**（总量 / 输出预留 / 各来源配额） |
| `partition.py` | **分区器**（五类来源 → 信封 → 构成表 + 裁剪原因） |
| `render.py` | **渲染器**（信封 → 提示词；`legacy` 逐字一致 / `structured` 分段） |

## 为什么从单文件变成包

单文件 `context.py` 只能放「证据装配」一件事；分区器与预算各有独立职责，
塞在一起会让「装配」与「分区」互相污染（谁该管裁剪？）。

**对外接口保持不变**：`from app.services.context import ContextItem, assemble_context`
仍然可用（`__init__` 里再导出），已有调用方（`services/pipeline.py`）零改动。

## 阶段边界

`ST-01` **阶段 A 只观测**：分区器用于测量与透出构成表，
**渲染输出与现有拼接逐字一致**（见 `partition.render_evidence_block`）。

`ST-01` **阶段 B 结构分区渲染**已落地在 `render.py`，由配置
`context_partition_render`（`legacy` | `structured`）选择；**默认仍是 `legacy`**——
切换默认值必须由 judge 前后对比数据驱动，而不是靠改代码。
"""
from __future__ import annotations

from .assembly import (
    CHARS_PER_TOKEN,
    ContextItem,
    ContextResult,
    assemble_context,
    estimate_tokens,
)
from .budget import (
    DEFAULT_OUTPUT_RESERVE_RATIO,
    SOURCE_QUOTA_RATIOS,
    ContextBudget,
    build_budget,
)
from .partition import (
    ContextEnvelope,
    ContextSource,
    PartitionItem,
    partition,
)

# 渲染器**不在这里再导出**：`render.py` 依赖 `generation/prompt.py` 的模板字面量，
# 顶层再导出会让 `context` 包在导入期就拉起 generation（`test_module_import_hygiene`
# 的顶层导入棘轮不允许）。调用方按需 `from app.services.context.render import ...`。

__all__ = [
    # 装配（原有）
    "CHARS_PER_TOKEN",
    "ContextItem",
    "ContextResult",
    "assemble_context",
    "estimate_tokens",
    # 预算
    "ContextBudget",
    "build_budget",
    "SOURCE_QUOTA_RATIOS",
    "DEFAULT_OUTPUT_RESERVE_RATIO",
    # 分区
    "ContextSource",
    "PartitionItem",
    "ContextEnvelope",
    "partition",
]
