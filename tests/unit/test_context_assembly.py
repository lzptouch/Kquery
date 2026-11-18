"""上下文组装治理测试（ / ）。

此前的问题：
1. 只按条数控制，无 token 预算——条数相同不代表长度相同；
2. 父块回查把多个子块替换成同一父块全文，替换后不重排不截断 → 同一段进 N 次；
3. 无多样性约束——Top-K 全来自同一文档相邻段落时覆盖面反而更窄。
"""
from app.services.context import (
    ContextItem,
    assemble_context,
    estimate_tokens,
)


def _item(key: str, content: str, score: float, doc: str = "d1", rank: int = 0):
    return ContextItem(key=key, doc_id=doc, content=content, score=score, rank=rank)


class TestDedupe:
    def test_same_key_kept_once(self):
        """父块展开后同一 parent 会出现多次——只能保留一条。"""
        items = [
            _item("parent:p1", "父块全文", 0.9, rank=1),
            _item("parent:p1", "父块全文", 0.8, rank=2),
            _item("parent:p1", "父块全文", 0.7, rank=3),
        ]
        r = assemble_context(items, token_budget=0)
        assert len(r.items) == 1
        assert len(r.dropped) == 2
        assert all("重复" in d.reason for d in r.dropped)

    def test_dedupe_can_be_disabled(self):
        items = [_item("k1", "x", 0.9), _item("k1", "x", 0.8)]
        r = assemble_context(items, token_budget=0, dedupe_keys=False)
        assert len(r.items) == 2


class TestTokenBudget:
    def test_budget_drops_lowest_scores_first(self):
        """超预算时先丢低分——高分证据必须优先保住。"""
        long_text = "字" * 400  # 约 200 token
        items = [
            _item("hi", long_text, 0.9, rank=1),
            _item("mid", long_text, 0.5, rank=2),
            _item("lo", long_text, 0.1, rank=3),
        ]
        r = assemble_context(items, token_budget=300, mmr_lambda=1.0)
        kept = {i.key for i in r.items}
        assert "hi" in kept, "高分证据必须保留"
        assert "lo" not in kept, "低分证据应先出局"
        assert any("超出 token 预算" in d.reason for d in r.dropped)

    def test_tokens_within_budget(self):
        items = [_item(f"k{i}", "字" * 200, 0.5, rank=i) for i in range(10)]
        r = assemble_context(items, token_budget=500, mmr_lambda=1.0)
        assert sum(estimate_tokens(i.content) for i in r.items) <= 500

    def test_zero_budget_means_unlimited(self):
        items = [_item(f"k{i}", "字" * 1000, 0.5, rank=i) for i in range(5)]
        r = assemble_context(items, token_budget=0, mmr_lambda=1.0)
        assert len(r.items) == 5

    def test_single_oversized_item_is_hard_truncated(self):
        """单条就超预算时必须硬截断并**标记**，而不是静默塞进去。"""
        items = [_item("big", "字" * 2000, 0.9, rank=1)]
        r = assemble_context(items, token_budget=100, mmr_lambda=1.0)
        assert r.truncated is True, "应标记为发生了截断"
        assert len(r.items) == 1, "唯一一条证据即使超预算也要保留截断版（否则上下文为空）"
        assert len(r.items[0].content) <= 100  # 中文密度 1 token/字 → 100 字上限
        # 截断原因必须留痕（措辞不固定，但必须说明"这条被裁过"）
        assert "截断" in r.items[0].reason or any("截断" in d.reason for d in r.dropped)

    def test_estimate_tokens_is_language_aware(self):
        """中英按字符类分别计，而不是取一个折中常数。

        旧实现是 `len/2`：中文 1 字实际约 1 token → 被低估一半（预算算完不超、
        发出去被上游截断）；英文 4 字符约 1 token → 被高估一半（大量预算闲置）。
        现在 CJK 记 1、其它每 4 字符记 1，两边都落在正确量级上。
        """
        assert estimate_tokens("abcd") == 1  # 4 字符 ≈ 1 token
        assert estimate_tokens("中文语料") == 4  # 4 字 ≈ 4 token
        assert estimate_tokens("hello 世界") == 4  # 2 + 2
        assert estimate_tokens("") == 0
        assert estimate_tokens("abcd" * 25) == 25
        assert estimate_tokens("中" * 100) == 100


class TestMmrDiversity:
    def test_mmr_prefers_diverse_over_duplicate(self):
        """两篇文档各一条 > 同一篇文档的两条近似段落（即使分数略低）。"""
        a1 = _item("a1", "苹果公司发布了新款手机，搭载全新芯片，性能提升明显。", 0.90, doc="A", rank=1)
        a2 = _item("a2", "苹果公司发布了新款手机，搭载全新芯片，性能提升显著。", 0.89, doc="A", rank=2)
        b1 = _item("b1", "香蕉富含钾元素，适合运动后补充电解质。", 0.70, doc="B", rank=3)

        # 条数上限由 token 预算推导（`A-21.4` 删掉了从不传的 `max_items` 参数）：
        # 给「刚好只够放两条」的预算，MMR 才需要在上限内挑覆盖面。
        budget = estimate_tokens(a1.content) + estimate_tokens(b1.content)
        diverse = assemble_context([a1, a2, b1], token_budget=budget, mmr_lambda=0.5)
        keys = {i.key for i in diverse.items}
        assert "a1" in keys and "b1" in keys, "MMR 应牺牲重复项换取覆盖面"
        assert "a2" not in keys

    def test_lambda_one_is_pure_relevance(self):
        a1 = _item("a1", "苹果发布新手机", 0.90, rank=1)
        a2 = _item("a2", "苹果发布新手机", 0.89, rank=2)  # 几乎重复但分数高
        b1 = _item("b1", "香蕉富含钾", 0.70, rank=3)
        budget = estimate_tokens(a1.content) + estimate_tokens(a2.content)
        pure = assemble_context([a1, a2, b1], token_budget=budget, mmr_lambda=1.0)
        assert {i.key for i in pure.items} == {"a1", "a2"}, "λ=1 时退化为纯按分数排序"

    def test_dropped_records_reason(self):
        items = [_item(f"k{i}", "完全不同的内容" + "字" * i, 0.9 - i * 0.1, rank=i) for i in range(5)]
        budget = estimate_tokens(items[0].content) + estimate_tokens(items[1].content)
        r = assemble_context(items, token_budget=budget, mmr_lambda=0.5)
        assert r.dropped, "被淘汰的候选必须留痕（前端要能解释为什么没进上下文）"
        assert all(d.reason for d in r.dropped)

    def test_items_sorted_by_rank(self):
        """输出按 rank 正序（与引用编号一致），不是按分数。"""
        items = [
            _item("c", "内容丙", 0.9, rank=3),
            _item("a", "内容甲", 0.5, rank=1),
            _item("b", "内容乙", 0.7, rank=2),
        ]
        r = assemble_context(items, token_budget=0, mmr_lambda=1.0)
        assert [i.rank for i in r.items] == [1, 2, 3]


class TestEdgeCases:
    def test_empty_input(self):
        r = assemble_context([])
        assert r.items == [] and r.tokens == 0

    def test_single_item(self):
        r = assemble_context([_item("only", "内容", 0.5, rank=1)], token_budget=100)
        assert len(r.items) == 1

    def test_no_budget_means_no_count_limit(self):
        """预算不限 = 条数不限（上限由预算推导，预算为 0 时不设限）。"""
        items = [_item(f"k{i}", "短", 0.5, rank=i) for i in range(20)]
        r = assemble_context(items, token_budget=0, mmr_lambda=1.0)
        assert len(r.items) == 20

    def test_count_limit_derived_from_budget(self):
        """预算只够放两条 → 最多留两条（其余进 dropped）。"""
        items = [_item(f"k{i}", "内容稍长一些的文本", 0.9 - i * 0.1, rank=i) for i in range(5)]
        cost = estimate_tokens(items[0].content)
        r = assemble_context(items, token_budget=cost * 2, mmr_lambda=1.0)
        assert len(r.items) <= 2
        assert r.dropped
