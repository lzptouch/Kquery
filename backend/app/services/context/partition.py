"""上下文分区器（`ST-01` 阶段 A · 只观测）。

## 解决的问题

「进 prompt 的东西」此前是**三处独立拼接**的结果，没有统一口径：

| 来源 | 注入位置（线性路径 / Agent 路径） |
|---|---|
| 工具定义 | `services/agent/planner.py` 的提示词模板 / `react.py` |
| 文档证据 | `services/pipeline.py` 的 `build_evidence_block` / `services/agent/runner.py` |
| 长期记忆 | `services/agent/memory.py` → `graph.py`（裸字符串 hint） |
| 历史对话 | `services/agent/state.py` 定义了但**没有注入点**（死字段） |

于是「哪些进了 prompt、各占多少 token」**无法回答**——只能靠翻代码猜。

## 阶段 A（本次落地）：只观测，不改渲染

分区器先只做两件事：

1. **枚举 + 计量**：把五个来源的内容装进信封，得出构成表（来源 / 条数 / token）；
2. **暴露裁剪原因**：预算不足时被裁掉的条目带原因。

**渲染输出与现有拼接逐字一致**（`render_evidence_block()` 与
`build_evidence_block()` 输出相同）——理由：改提示词结构会改变答案质量，
而生成质量的测量（`EV-03` 的 judge）此时还没建，「改完没感觉变好」无法被证伪。

> 阶段 B（结构分区）等 `EV-03` 落地后再做，届时用 judge 维度分做前后对比门禁。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .assembly import ContextItem, estimate_tokens
from .budget import ContextBudget


class ContextSource(str, Enum):
    """进 prompt 的五个来源。"""

    SYSTEM = "system"      # 系统约束（角色、边界声明）
    HISTORY = "history"    # 压缩后的历史对话
    EVIDENCE = "evidence"  # 检索证据
    TOOLS = "tools"        # 工具目录
    MEMORY = "memory"      # 租户级长期记忆


@dataclass
class PartitionItem:
    """一个分区条目。"""

    source: ContextSource
    content: str
    tokens: int
    pinned: bool = False   # 保底段：预算不足时仍保留
    priority: int = 0      # 裁剪优先级（越小越先被裁）
    key: str = ""          # 身份标识（chunk_id / parent_id）
    label: str = ""        # 展示标签（「手册.md · 第2页」）——与身份**不是一回事**
    doc_id: str = ""       # 所属文档（渲染时用于判断「换文档了要加分隔线」）
    reason: str = ""       # 被裁时的原因（回传给前端解释「为什么没进上下文」）

    def as_dict(self) -> dict:
        return {
            "source": self.source.value,
            "key": self.key,
            "label": self.label,
            "tokens": self.tokens,
            "pinned": self.pinned,
            "priority": self.priority,
            "reason": self.reason,
        }


@dataclass
class ContextEnvelope:
    """分区信封：分区条目 + 预算 + 丢弃原因。"""

    items: list[PartitionItem] = field(default_factory=list)
    dropped: list[PartitionItem] = field(default_factory=list)
    budget: ContextBudget | None = None

    @property
    def tokens(self) -> int:
        return sum(i.tokens for i in self.items)

    def by_source(self) -> dict[str, list[PartitionItem]]:
        out: dict[str, list[PartitionItem]] = {s.value: [] for s in ContextSource}
        for item in self.items:
            out[item.source.value].append(item)
        return out

    def breakdown(self) -> dict:
        """构成表（导出给 `/admin/metrics/context` 与响应体）。

        五个来源键**齐全**（没有内容的来源给 0），否则前端要处理「键时有时无」。
        """
        result: dict = {
            "total_tokens": self.tokens,
            "budget_tokens": self.budget.total_tokens if self.budget else 0,
            "available_tokens": self.budget.available_tokens if self.budget else 0,
            "sources": {},
            "dropped": [],
        }
        for source, items in self.by_source().items():
            result["sources"][source] = {
                "count": len(items),
                "tokens": sum(i.tokens for i in items),
                "quota": self.budget.quota_for(source) if self.budget else 0,
            }
        result["dropped"] = [
            {"source": i.source.value, "key": i.key, "tokens": i.tokens, "reason": i.reason}
            for i in self.dropped
        ]
        return result

    def render_evidence_block(self) -> str:
        """渲染证据段（阶段 A：与现有 `build_evidence_block` **逐字一致**）。

        这里**不自己拼**——直接委托给既有函数，保证「只观测」承诺成立。
        """
        # 破环：pipeline 反向依赖 context（装配 + 分区），顶层导入会成环
        from app.domain.schemas import Evidence
        from app.services.pipeline import build_evidence_block

        items = [i for i in self.items if i.source is ContextSource.EVIDENCE]
        evidence = [
            Evidence(
                rank=idx,
                chunk_id=item.key,
                content=item.content,
                source=item.label or item.key,
                doc_id=item.doc_id,
                filename=item.key,
                used=True,
            )
            for idx, item in enumerate(items, start=1)
        ]
        return build_evidence_block(evidence)


def partition(
    *,
    budget: ContextBudget,
    system: str = "",
    history: list[str] | None = None,
    evidence: list[ContextItem] | None = None,
    tools: str = "",
    memory: str = "",
) -> ContextEnvelope:
    """把五个来源装进信封（按配额裁剪，被裁的记原因）。

    裁剪顺序：**先裁可选段**（按优先级从低到高），保底段（`pinned`）永不裁。
    """
    envelope = ContextEnvelope(budget=budget)

    entries: list[PartitionItem] = []
    if system:
        entries.append(
            PartitionItem(
                source=ContextSource.SYSTEM, content=system,
                tokens=estimate_tokens(system), pinned=True, priority=0, key="system",
            )
        )
    if tools:
        entries.append(
            PartitionItem(
                source=ContextSource.TOOLS, content=tools,
                tokens=estimate_tokens(tools), pinned=False, priority=1, key="tools",
            )
        )
    if memory:
        entries.append(
            PartitionItem(
                source=ContextSource.MEMORY, content=memory,
                tokens=estimate_tokens(memory), pinned=False, priority=2, key="memory",
            )
        )
    for idx, text in enumerate(history or []):
        entries.append(
            PartitionItem(
                source=ContextSource.HISTORY, content=text,
                tokens=estimate_tokens(text), pinned=False, priority=3, key=f"history#{idx}",
            )
        )
    for item in evidence or []:
        entries.append(
            PartitionItem(
                source=ContextSource.EVIDENCE, content=item.content,
                tokens=estimate_tokens(item.content), pinned=False, priority=4,
                key=item.key or item.doc_id,
                # 展示标签（「手册.md · 第2页」）与身份（chunk_id）**不是一回事**：
                # 渲染时用的是标签——阶段 A 的逐字一致门禁就靠它。
                label=item.source, doc_id=item.doc_id,
            )
        )

    # 按来源配额裁剪（保底段不参与裁剪）
    kept: list[PartitionItem] = []
    for source in ContextSource:
        group = [e for e in entries if e.source is source]
        if not group:
            continue
        quota = budget.quota_for(source.value)
        used = 0
        for entry in sorted(group, key=lambda e: e.priority):
            if entry.pinned:
                kept.append(entry)
                continue
            if used + entry.tokens <= quota:
                kept.append(entry)
                used += entry.tokens
                continue
            entry.reason = f"超出 {source.value} 配额（{quota} tokens）"
            envelope.dropped.append(entry)

    # 保持稳定顺序：来源枚举顺序 → 原始条目顺序
    order = {s: i for i, s in enumerate(ContextSource)}
    kept.sort(key=lambda e: (order[e.source], e.priority, e.key))
    envelope.items = kept
    return envelope
