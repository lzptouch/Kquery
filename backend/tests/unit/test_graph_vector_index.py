"""实体向量索引（消歧与种子召回去线性扫描）。

关键约束：图谱消歧**不允许近似**——漏合并会导致同一实体被拆成多个，
错误合并会把不相关实体混为一谈。因此这里刻意用向量化精确检索而非 ANN，
测试必须验证"结果与逐个点积完全一致"。
"""
import numpy as np
import pytest

from app.services.graph.vector_index import EntityVectorIndex


def _vec(*values) -> np.ndarray:
    return np.array(values, dtype=np.float32)


class TestExactnessMatchesNaive:
    """与朴素逐个点积的结果必须一致（消歧不允许近似）。"""

    @pytest.mark.parametrize("n", [1, 5, 40, 200])
    def test_top1_matches_naive(self, n):
        rng = np.random.default_rng(42)
        dim = 16
        idx = EntityVectorIndex()
        vecs = {}
        for i in range(n):
            v = rng.random(dim).astype(np.float32)
            v /= np.linalg.norm(v)
            name = f"e{i}"
            vecs[name] = v
            idx.add(name, v)

        query = rng.random(dim).astype(np.float32)
        query /= np.linalg.norm(query)
        got = idx.search(query, top_k=1)[0]

        naive = max(((nm, float(np.dot(query, v))) for nm, v in vecs.items()),
                    key=lambda x: x[1])
        assert got[0] == naive[0], "向量化检索的 top-1 必须与朴素点积相同"
        assert got[1] == pytest.approx(naive[1], abs=1e-6)

    def test_topk_set_matches_naive(self):
        rng = np.random.default_rng(7)
        idx = EntityVectorIndex()
        vecs = {}
        for i in range(50):
            v = rng.random(8).astype(np.float32)
            vecs[f"e{i}"] = v
            idx.add(f"e{i}", v)
        q = rng.random(8).astype(np.float32)

        got = {nm for nm, _ in idx.search(q, top_k=5)}
        naive = {nm for nm, _ in sorted(
            ((nm, float(np.dot(q, v))) for nm, v in vecs.items()),
            key=lambda x: -x[1])[:5]}
        assert got == naive


class TestIndexMaintenance:
    def test_duplicate_add_overwrites(self):
        """同名重复添加应覆盖向量而不是产生两条（图谱会重复 upsert 同一实体）。"""
        idx = EntityVectorIndex()
        idx.add("a", _vec(1.0, 0.0))
        idx.add("b", _vec(0.0, 1.0))
        idx.add("a", _vec(0.0, 1.0))  # a 的向量被改写
        assert len(idx) == 2, "重复添加不应增加实体数"
        hits = idx.search(_vec(0.0, 1.0), top_k=2)
        assert {nm for nm, _ in hits} == {"a", "b"}

    def test_growth_beyond_initial_capacity(self):
        """初始容量 64，超过后应自动扩容且不丢数据。"""
        idx = EntityVectorIndex()
        for i in range(500):
            idx.add(f"e{i}", _vec(float(i), 1.0))
        assert len(idx) == 500
        hits = idx.search(_vec(499.0, 1.0), top_k=1)
        assert hits[0][0] == "e499"

    def test_threshold_filters(self):
        idx = EntityVectorIndex()
        idx.add("a", _vec(1.0, 0.0))
        idx.add("b", _vec(0.0, 1.0))
        hits = idx.search(_vec(1.0, 0.0), top_k=5, threshold=0.9)
        assert [h[0] for h in hits] == ["a"]

    def test_type_filter_applies(self):
        """消歧的类型硬约束：只和同类型实体比。"""
        idx = EntityVectorIndex()
        idx.add("person_x", _vec(1.0, 0.0))
        idx.add("org_y", _vec(1.0, 0.0))  # 向量完全一致，但类型不同
        types = {"person_x": "person", "org_y": "org"}

        hits = idx.search(_vec(1.0, 0.0), top_k=1, type_of=types, want_type="org")
        assert hits[0][0] == "org_y"

        hits = idx.search(_vec(1.0, 0.0), top_k=1, type_of=types, want_type="person")
        assert hits[0][0] == "person_x"

    def test_empty_index_returns_empty(self):
        assert EntityVectorIndex().search(_vec(1.0, 0.0)) == []

    def test_dim_mismatch_returns_empty_not_crash(self):
        """维度不一致（换了 embedding 模型）应安全返回空，而不是抛异常。"""
        idx = EntityVectorIndex()
        idx.add("a", _vec(1.0, 0.0, 0.0))
        assert idx.search(_vec(1.0, 0.0)) == []

    def test_batch_suspend_resume(self):
        """批量写入：suspend 期间暂存，resume 后一次落矩阵且结果正确。"""
        idx = EntityVectorIndex()
        idx.suspend()
        for i in range(100):
            idx.add(f"e{i}", _vec(float(i), 0.0))
        idx.resume()
        assert len(idx) == 100
        assert idx.search(_vec(99.0, 0.0), top_k=1)[0][0] == "e99"

    def test_add_after_resume_works(self):
        idx = EntityVectorIndex()
        idx.suspend()
        idx.add("a", _vec(1.0, 0.0))
        idx.resume()
        idx.add("b", _vec(0.0, 1.0))
        assert len(idx) == 2


class TestResolverUsesIndex:
    """消歧路径接入索引后的行为等价性（不能因优化而改变判定结果）。"""

    class _Emb:
        def __init__(self, table):
            self._t = table

        def embed_query(self, text):
            return self._t.get(text, [0.0, 0.0])

    def test_alias_hit_unchanged(self):
        from app.services.graph.resolver import EntityResolver

        r = EntityResolver(self._Emb({"苹果公司": [1.0, 0.0], "苹果": [1.0, 0.0]}), sim_threshold=0.9)
        canonical, created = r.resolve("苹果公司", "org")
        assert created is True
        _, created2 = r.resolve("苹果", "org")  # 向量一致 → 合并
        assert created2 is False
        assert r.lookup("苹果") == canonical

    def test_different_type_not_merged(self):
        """类型不同不合并（§5.4 硬约束）——优化后必须保持。"""
        from app.services.graph.resolver import EntityResolver

        r = EntityResolver(self._Emb({"苹果公司": [1.0, 0.0], "苹果": [1.0, 0.0]}), sim_threshold=0.9)
        r.resolve("苹果公司", "org")
        _, created = r.resolve("苹果", "fruit")
        assert created is True, "类型不同不得合并"

    def test_below_threshold_creates_new(self):
        from app.services.graph.resolver import EntityResolver

        r = EntityResolver(self._Emb({"甲": [1.0, 0.0], "乙": [0.0, 1.0]}), sim_threshold=0.9)
        r.resolve("甲", "org")
        _, created = r.resolve("乙", "org")
        assert created is True

    def test_search_similar_reuses_index(self):
        from app.services.graph.resolver import EntityResolver

        r = EntityResolver(self._Emb({"甲": [1.0, 0.0], "乙": [0.0, 1.0]}), sim_threshold=0.9)
        r.resolve("甲", "org")
        r.resolve("乙", "org")
        hits = r.search_similar(np.array([1.0, 0.0], dtype=np.float32), top_k=2, threshold=0.0)
        assert hits[0][0] == "甲"
