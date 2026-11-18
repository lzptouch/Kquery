"""上下文分区器（`ST-01` 阶段 A · 只观测）。

## 五条验收（设计文档 §2.1）

① 五类来源各自计入正确配额
② 预算不足时**保底段**仍保留
③ 可选段被裁且**丢弃原因非空**
④ **阶段 A 门禁**：渲染产出与现有拼接**逐字一致**
⑤ 同一输入两次分区结果**完全一致**

## 为什么第 ④ 条最重要

阶段 A 的承诺是「只观测，不改渲染」。如果渲染变了，答案质量就会变——
而此时生成质量的测量（`EV-03` 的 judge）还没建，
「改完没感觉变好」**无法被证伪**。所以这条断言是整个阶段 A 的守门人。
"""
from __future__ import annotations

from app.services.context import (
    ContextItem,
    ContextSource,
    build_budget,
    partition,
)
from app.services.context.budget import SOURCE_QUOTA_RATIOS
from app.services.pipeline import build_evidence_block


def _evidence(n: int, size: int = 100) -> list[ContextItem]:
    return [
        ContextItem(
            key=f"c{i}", doc_id="d1", content="证" * size, score=1.0 - i / 100,
            source="手册.md · 第1页", rank=i,
        )
        for i in range(n)
    ]


def test_five_sources_each_get_their_own_quota():
    """① 五类来源各自计入正确配额。"""
    budget = build_budget(6000)
    assert set(budget.quotas) == set(SOURCE_QUOTA_RATIOS)
    for source, ratio in SOURCE_QUOTA_RATIOS.items():
        assert budget.quota_for(source) > 0, source
    # 配额之和 = 可用 token（取整的差额补给最大的来源）
    assert sum(budget.quotas.values()) == budget.available_tokens

    env = partition(
        budget=budget,
        system="系统约束",
        history=["历史1", "历史2"],
        evidence=_evidence(3),
        tools="工具目录",
        memory="记忆",
    )
    breakdown = env.breakdown()
    assert set(breakdown["sources"]) == {"system", "history", "evidence", "tools", "memory"}
    for source, info in breakdown["sources"].items():
        assert info["quota"] == budget.quota_for(source), source
    assert breakdown["sources"]["evidence"]["count"] == 3
    assert breakdown["sources"]["history"]["count"] == 2


def test_pinned_section_survives_budget_pressure():
    """② 预算不足时保底段（system）仍保留。"""
    budget = build_budget(100)  # 极小预算
    env = partition(
        budget=budget,
        system="系统约束（保底）",
        evidence=_evidence(20, size=500),  # 远超配额
    )
    systems = [i for i in env.items if i.source is ContextSource.SYSTEM]
    assert systems, "保底段必须保留"
    evidence_kept = [i for i in env.items if i.source is ContextSource.EVIDENCE]
    assert len(evidence_kept) < 20, "超预算的证据应被裁"


def test_dropped_items_carry_a_reason():
    """③ 可选段被裁且丢弃原因非空。"""
    budget = build_budget(200)
    env = partition(budget=budget, evidence=_evidence(50, size=100))
    assert env.dropped, "应有条目被裁"
    for item in env.dropped:
        assert item.reason, "丢弃原因不能为空（前端要解释「为什么没进上下文」）"
    assert "配额" in env.dropped[0].reason
    # 构成表里也要能看到丢弃条目
    breakdown = env.breakdown()
    assert breakdown["dropped"]
    assert all(d["reason"] for d in breakdown["dropped"])


def test_stage_a_rendering_is_byte_identical():
    """④ **阶段 A 门禁**：分区器的渲染与现有拼接逐字一致。"""
    from app.domain.schemas import Evidence

    budget = build_budget(6000)
    used = [
        Evidence(rank=1, chunk_id="c1", score=0.9, content="证据一",
                 source="a.md", doc_id="d1", filename="a.md", used=True),
        Evidence(rank=2, chunk_id="c2", score=0.8, content="证据二",
                 source="b.md", doc_id="d2", filename="b.md", used=True),
    ]
    env = partition(
        budget=budget,
        evidence=[
            ContextItem(key=e.chunk_id, doc_id=e.doc_id, content=e.content, source=e.source)
            for e in used
        ],
    )
    # 既有渲染（生产路径）
    legacy = build_evidence_block(used)
    # 分区器渲染（阶段 A 必须逐字相同）
    assert env.render_evidence_block() == legacy


def test_partition_is_deterministic():
    """⑤ 同一输入两次分区结果完全一致。"""
    budget = build_budget(6000)
    kwargs = dict(
        budget=budget, system="系统", history=["h1"], evidence=_evidence(5), tools="t", memory="m",
    )
    first = partition(**kwargs)
    second = partition(**kwargs)
    assert first.breakdown() == second.breakdown()
    assert [i.key for i in first.items] == [i.key for i in second.items]


def test_budget_derives_from_total_with_output_reserve():
    """预算是单一真相源：总量 → 输出预留 → 各来源配额。"""
    budget = build_budget(1000)
    assert budget.total_tokens == 1000
    assert budget.output_reserve_tokens == 250  # 25%
    assert budget.available_tokens == 750
    assert sum(budget.quotas.values()) == 750


def test_context_package_keeps_public_api():
    """单文件 → 包之后，对外接口不能变（已有调用方零改动）。"""
    import app.services.context as ctx

    assert hasattr(ctx, "ContextItem")
    assert hasattr(ctx, "assemble_context")
    assert hasattr(ctx, "estimate_tokens")
    # 装配的既有行为不变
    result = ctx.assemble_context(
        [ctx.ContextItem(key="c1", doc_id="d1", content="正文", score=1.0)],
        token_budget=1000,
    )
    assert len(result.items) == 1
