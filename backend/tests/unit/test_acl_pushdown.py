"""ACL 下推与超额取数。

要解决的问题：三个检索后端都是"先按分数取 N 条，再过滤"。
加入 ACL 后，排名靠前的不可见文档会**占满 top-k 名额**：

    用户可读 10 份中的 7 份，不可读的 3 份恰好排在前面
    → 取 top-4 → 3 条被过滤 → 只剩 1 条
    → 极端情况可见证据为 0 → 误判"无权限"拒答

用户明明有权看那 7 份，却因为"别人/旧的不可见文档占了名额"而拿不到答案。
"""
import numpy as np

from app.services.retrieval.filters import (
    MAX_OVERFETCH_FACTOR,
    PUSHDOWN_MAX_DOCS,
    OverfetchState,
    RetrievalFilters,
)


class TestRetrievalFilters:
    def test_allows_checks_tenant_doc_and_acl(self):
        f = RetrievalFilters(tenant_id="t1", visible_doc_ids={"a", "b"})
        assert f.allows("a", "t1") is True
        assert f.allows("c", "t1") is False, "不在允许清单内应拒绝"
        assert f.allows("a", "t2") is False, "跨租户应拒绝"

    def test_none_allowlist_means_no_doc_restriction(self):
        """visible_doc_ids=None 表示 ACL 未启用，不应做文档级限制。"""
        f = RetrievalFilters(tenant_id="t1", visible_doc_ids=None)
        assert f.allows("anything", "t1") is True

    def test_doc_id_pins_single_document(self):
        f = RetrievalFilters(tenant_id="t1", doc_id="a")
        assert f.allows("a", "t1") is True
        assert f.allows("b", "t1") is False

    def test_empty_allowlist_rejects_everything(self):
        """空允许清单 = 没有任何可见文档（必须恒假，而不是"不过滤"）。"""
        f = RetrievalFilters(tenant_id="t1", visible_doc_ids=set())
        assert f.allows("a", "t1") is False

    def test_milvus_expr_pushes_allowlist(self):
        f = RetrievalFilters(tenant_id="t1", visible_doc_ids={"a", "b"})
        expr = f.to_milvus_expr()
        assert 'tenant_id == "t1"' in expr
        assert "doc_id in [" in expr and '"a"' in expr and '"b"' in expr

    def test_milvus_expr_degrades_when_allowlist_too_large(self):
        """清单过大时不构造 in [...]——超长表达式会让 Milvus 直接报错。

        这比"少过滤一点"严重得多：报错会中断整次检索，而降级只是损失一部分
        下推收益（精确性仍由应用层后过滤保证）。
        """
        big = {f"d{i}" for i in range(PUSHDOWN_MAX_DOCS + 1)}
        f = RetrievalFilters(tenant_id="t1", visible_doc_ids=big)
        expr = f.to_milvus_expr()
        assert "doc_id in [" not in expr
        assert f.pushdown_degraded() is True

    def test_milvus_expr_empty_allowlist_is_always_false(self):
        f = RetrievalFilters(tenant_id="t1", visible_doc_ids=set())
        assert '__none__' in f.to_milvus_expr()

    def test_expr_escapes_quotes(self):
        """表达式转义：不做转义等于把注入面留在原地。"""
        f = RetrievalFilters(tenant_id='t"1')
        assert '\\"' in f.to_milvus_expr()

    def test_extra_conditions_appended(self):
        f = RetrievalFilters(tenant_id="t1")
        expr = f.to_milvus_expr(extra=["is_current_version == true"])
        assert "is_current_version == true" in expr


class TestOverfetchState:
    def test_factor_grows_and_caps(self):
        s = OverfetchState()
        for _ in range(20):
            s.next_limit(10)
        assert s.factor <= MAX_OVERFETCH_FACTOR, "扩取倍数必须有上限（避免全量扫描）"

    def test_continue_until_satisfied(self):
        s = OverfetchState()
        assert s.can_continue(wanted=5, got=2, total=1000) is True
        assert s.can_continue(wanted=5, got=5, total=1000) is False

    def test_stops_when_candidates_exhausted(self):
        """候选已耗尽时不再扩取，并标记 exhausted（区分"取不到"与"本来就少"）。"""
        s = OverfetchState()
        s.fetched = 1000
        assert s.can_continue(wanted=5, got=1, total=1000) is False
        assert s.exhausted is True


class _StubStore:
    """最小可用的向量存储替身：只实现本测试需要的接口。"""

    def __init__(self, order: list[str], tenant_of: dict, total: int | None = None):
        self._order = order
        self._tenant = tenant_of
        self._total = total if total is not None else len(order)

    def search(self, vec, top_k, tenant_id, doc_id=None, filters=None):
        # 按 order 顺序模拟"排名"：前面的分数更高
        out = []
        for i, doc in enumerate(self._order):
            if len(out) >= top_k:
                break
            if self._tenant.get(doc) != tenant_id:
                continue
            if filters is not None and not filters.allows(doc, self._tenant.get(doc, "")):
                continue
            out.append((doc, 1.0 - i * 0.001))
        return out


class TestVisibleEvidenceNotStarved:
    """核心验收：可见证据不得被不可见文档占满名额。"""

    def test_post_filter_would_starve_without_pushdown(self):
        """反例（改造前的行为）：固定 4 倍超额取 + 大量不可见文档 → 可见证据为 0。"""
        order = [f"hidden{i}" for i in range(50)] + ["visible1", "visible2"]
        tenant = {**{f"hidden{i}": "t1" for i in range(50)}, "visible1": "t1", "visible2": "t1"}
        store = _StubStore(order, tenant)

        # 模拟"后过滤"：取 4 倍候选，再按 ACL 过滤
        raw = store.search([0.0], top_k=4 * 5, tenant_id="t1", filters=None)
        allowed = {"visible1", "visible2"}
        after = [d for d, _ in raw if d in allowed]
        # 固定 4 倍只取到 20 条，全被不可见文档占据
        assert after == [], "这正是 后过滤把可见证据吃光了"

    def test_pushdown_returns_visible_evidence(self):
        """正例（改造后）：允许清单下推 → 不可见文档不参与竞争。"""
        order = [f"hidden{i}" for i in range(50)] + ["visible1", "visible2"]
        tenant = {**{f"hidden{i}": "t1" for i in range(50)}, "visible1": "t1", "visible2": "t1"}
        store = _StubStore(order, tenant)

        filters = RetrievalFilters(tenant_id="t1", visible_doc_ids={"visible1", "visible2"})
        got = store.search([0.0], top_k=5, tenant_id="t1", filters=filters)

        assert [d for d, _ in got] == ["visible1", "visible2"], (
            "下推后必须取到可见文档（即使它们排名靠后）"
        )


class TestFaissIterativeOverfetch:
    """FAISS 不支持表达式过滤，靠迭代扩取兜住（ 第一道防线）。"""

    def _store(self, tmp_path, hidden: int, visible: int):
        from app.domain.schemas import Chunk
        from app.services.vectorstore import VectorStore

        dim = 8
        store = VectorStore(tmp_path / "idx", dim)
        chunks, vecs = [], []
        # 先加不可见（分数更高：向量与查询更接近），再加可见
        for i in range(hidden):
            chunks.append(Chunk(chunk_id=f"h{i}#0", doc_id=f"hidden{i}", seq=0,
                                content=f"隐藏{i}", tenant_id="t1", source="s"))
            v = np.zeros(dim, dtype=np.float32)
            v[0] = 1.0
            v[1] = i * 1e-4
            vecs.append(v)
        for i in range(visible):
            chunks.append(Chunk(chunk_id=f"v{i}#0", doc_id=f"visible{i}", seq=0,
                                content=f"可见{i}", tenant_id="t1", source="s"))
            v = np.zeros(dim, dtype=np.float32)
            v[0] = 0.5
            v[1] = i * 1e-4
            vecs.append(v)
        store.add(chunks, vecs)
        return store

    def test_iterative_overfetch_finds_visible_docs(self, tmp_path):
        """不可见文档数量远超固定 4 倍时，迭代扩取仍应取到可见证据。"""
        store = self._store(tmp_path, hidden=40, visible=3)
        query = np.zeros(8, dtype=np.float32)
        query[0] = 1.0

        filters = RetrievalFilters(tenant_id="t1",
                                   visible_doc_ids={"visible0", "visible1", "visible2"})
        got = store.search(query.tolist(), top_k=3, tenant_id="t1", filters=filters)

        assert len(got) == 3, f"迭代扩取应取满 3 条可见证据，实得 {len(got)}"
        assert all(c.doc_id.startswith("visible") for c, _ in got)

    def test_no_filter_behaviour_unchanged(self, tmp_path):
        """不传 filters 时行为与改造前一致（返回分数最高的 top_k）。"""
        store = self._store(tmp_path, hidden=3, visible=3)
        query = np.zeros(8, dtype=np.float32)
        query[0] = 1.0
        got = store.search(query.tolist(), top_k=2, tenant_id="t1")
        assert len(got) == 2
        assert all(c.doc_id.startswith("hidden") for c, _ in got), "应返回分数最高者"

    def test_empty_result_when_nothing_allowed(self, tmp_path):
        store = self._store(tmp_path, hidden=3, visible=3)
        query = np.zeros(8, dtype=np.float32)
        query[0] = 1.0
        filters = RetrievalFilters(tenant_id="t1", visible_doc_ids=set())
        assert store.search(query.tolist(), top_k=3, tenant_id="t1", filters=filters) == []


class TestBm25FilterBeforeRanking:
    """BM25 改为**先过滤再排序**（此前是全量排序后逐个跳过）。"""

    def test_visible_docs_ranked_first(self):
        from app.domain.schemas import Chunk
        from app.services.retrieval.bm25 import BM25Retriever

        bm25 = BM25Retriever()
        chunks = [
            Chunk(chunk_id=f"h{i}#0", doc_id=f"hidden{i}", seq=0,
                  content="苹果 价格 苹果 苹果", tenant_id="t1", source="s")
            for i in range(10)
        ] + [
            Chunk(chunk_id="v#0", doc_id="visible", seq=0,
                  content="苹果 价格", tenant_id="t1", source="s")
        ]
        bm25.rebuild(chunks)

        filters = RetrievalFilters(tenant_id="t1", visible_doc_ids={"visible"})
        got = bm25.search("苹果 价格", 3, "t1", filters=filters)

        assert [c.chunk.doc_id for c in got] == ["visible"], (
            "不可见文档参与排序会占位；应先过滤再排序"
        )

    def test_tenant_isolation_kept(self):
        from app.domain.schemas import Chunk
        from app.services.retrieval.bm25 import BM25Retriever

        bm25 = BM25Retriever()
        bm25.rebuild([
            Chunk(chunk_id="a#0", doc_id="a", seq=0, content="机密 数据", tenant_id="t1", source="s"),
            Chunk(chunk_id="b#0", doc_id="b", seq=0, content="机密 数据", tenant_id="t2", source="s"),
        ])
        got = bm25.search("机密 数据", 5, "t2", filters=RetrievalFilters(tenant_id="t2"))
        assert [c.chunk.doc_id for c in got] == ["b"]
