"""Agent 路径的统一上下文组装（`ST-03`）。

## 三条验收

1. Agent 证据的 token 数**受上下文预算约束**
2. 同一父块的多条子块命中**只保留一条**
3. 被裁条目在**丢弃列表**里有原因
"""
from __future__ import annotations

from app.services.agent.runner import agent_prompt_builder
from app.services.context.expand import expand_parents


class _Parent:
    def __init__(self, content: str):
        self.content = content


def _evidence(chunk_id: str, content: str, parent_id: str = "", score: float = 1.0) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": "d1",
        "content": content,
        "parent_id": parent_id,
        "score": score,
        "source": "手册.md · 第1页",
    }


def test_same_parent_keeps_only_one():
    """② 同一父块的多条子块命中只保留一条（去重键 = 父块 ID）。"""
    items = [
        _evidence("c1", "子块一", parent_id="p1", score=0.9),
        _evidence("c2", "子块二", parent_id="p1", score=0.8),
    ]
    kept, dropped = expand_parents(items, lambda pid: _Parent("父块全文"))
    assert len(kept) == 1
    assert kept[0].content == "父块全文"
    assert kept[0].expanded is True
    assert dropped and "duplicate_parent" in dropped[0]["reason"]


def test_different_parents_are_all_kept():
    """不同父块都保留。"""
    items = [
        _evidence("c1", "子块一", parent_id="p1", score=0.9),
        _evidence("c2", "子块二", parent_id="p2", score=0.8),
    ]
    kept, dropped = expand_parents(items, lambda pid: _Parent(f"父块 {pid}"))
    assert len(kept) == 2
    assert not dropped


def test_missing_parent_falls_back_to_child_content():
    """取不到父块 → 退回子块内容（不让一条证据失效）。"""
    items = [_evidence("c1", "子块内容", parent_id="p1")]
    kept, _ = expand_parents(items, lambda pid: None)
    assert kept[0].content == "子块内容"
    assert kept[0].expanded is False


def test_agent_evidence_respects_token_budget():
    """① Agent 证据受上下文预算约束（此前**无上限**）。"""
    evidence = [_evidence(f"c{i}", "内容" * 200, score=1.0 - i / 100) for i in range(20)]
    dropped: list[dict] = []
    prompt = agent_prompt_builder(
        "问题", evidence, token_budget=200, dropped_out=dropped
    )
    # 预算很小 → 多数证据被裁
    assert dropped, "预算内的证据应被裁掉并记录原因"
    assert all(d.get("reason") for d in dropped)
    # 提示词里的证据条数明显少于 20 条
    assert prompt.count("（来源：") < 20


def test_no_budget_keeps_all_evidence():
    """不传预算 → 行为与改造前一致（全留）。"""
    evidence = [_evidence(f"c{i}", "内容", score=1.0) for i in range(5)]
    dropped: list[dict] = []
    prompt = agent_prompt_builder("问题", evidence, dropped_out=dropped)
    assert prompt.count("（来源：") == 5
    assert not dropped


def test_dropped_items_are_reported():
    """③ 被裁条目在丢弃列表里有原因。"""
    evidence = [_evidence(f"c{i}", "内容" * 200, score=1.0) for i in range(10)]
    dropped: list[dict] = []
    agent_prompt_builder("问题", evidence, token_budget=150, dropped_out=dropped)
    assert dropped
    for item in dropped:
        assert item["key"]
        assert item["reason"]
