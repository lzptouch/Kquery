"""向量索引类型（`A-26`）：默认 HNSW、可切 FLAT、旧索引自动升级。

## 为什么默认值要从 FLAT 换成 HNSW

FLAT 是**全量精确比较**：每次查询都遍历库内全部向量。召回没问题，但延迟随
库规模线性上涨——10 万块时单次查询要算 10 万次内积，而 RAG 的关键路径上
这一步是同步的。HNSW 用图结构把比较次数降到对数级，代价是**近似**：
极小概率漏掉真邻居，用 `ef_search` 换回来。

换默认值有三个必须一起解决的事，否则等于埋坑：

1. **配置要真正生效**。改动只落在 `VectorStore.__init__` 而不看已存在的索引文件，
   用户改了 `VECTOR_INDEX_TYPE=hnsw` 仍在跑 FLAT，且没有任何提示。
2. **删除路径要跟着换**。现有删除是「重建索引」（FAISS 的 FLAT 不支持按 id 删，
   所以拆出来重建），重建目标如果还写死 FLAT，一删就退化。
3. **精度要有对照**。HNSW 是近似算法，必须能证明"在本库规模下与 FLAT 同解"，
   否则"换了索引之后搜不到"会变成玄学。
"""
from pathlib import Path

import numpy as np

from app.domain.schemas import Chunk


def _chunk(cid: str, doc_id: str = "doc") -> Chunk:
    return Chunk(
        chunk_id=cid, doc_id=doc_id, seq=1, content=f"内容 {cid}",
        source="测试", tenant_id="t1",
    )


def _store(tmp_path, **kwargs):
    from app.services.vectorstore import VectorStore

    return VectorStore(Path(tmp_path), dim=8, **kwargs)


class TestDefaultIndexType:
    def test_default_is_hnsw(self, tmp_path):
        import faiss

        store = _store(tmp_path)
        assert isinstance(store._index, faiss.IndexHNSWFlat)
        assert store._index.hnsw.efSearch == 64

    def test_flat_is_still_selectable(self, tmp_path):
        import faiss

        store = _store(tmp_path, index_type="flat")
        assert isinstance(store._index, faiss.IndexFlatIP)

    def test_unknown_value_falls_back_to_hnsw(self, tmp_path):
        """拼错配置不该静默退化成最慢的那个（默认应偏性能侧）。"""
        import faiss

        store = _store(tmp_path, index_type="HSNW")  # 打错字
        assert isinstance(store._index, faiss.IndexHNSWFlat)

    def test_delete_rebuild_keeps_hnsw(self, tmp_path):
        """删除走「重建」路径：重建目标必须也是 HNSW，否则删一次就退化。"""
        import faiss

        store = _store(tmp_path)
        store.add([_chunk("a"), _chunk("b"), _chunk("c")], [[0.1] * 8 for _ in range(3)])
        store.delete_chunks({"b"})

        assert store.total == 2
        assert isinstance(store._index, faiss.IndexHNSWFlat)


class TestLegacyIndexUpgrade:
    def test_flat_index_is_upgraded_on_load(self, tmp_path):
        """存量索引是 FLAT、配置要 HNSW → 加载时就地重建，而不是静默继续用 FLAT。"""
        import faiss

        legacy = _store(tmp_path, index_type="flat")
        chunks = [_chunk(f"c{i}") for i in range(20)]
        legacy.add(chunks, [[float(i), 0.0] + [0.0] * 6 for i in range(20)])
        legacy.save()
        assert legacy.total == 20

        reloaded = _store(tmp_path)  # 默认要 hnsw
        reloaded.load()

        assert isinstance(reloaded._index, faiss.IndexHNSWFlat)
        assert reloaded.total == 20, "迁移不得丢向量"
        assert reloaded.doc_chunk_ids("doc") == [c.chunk_id for c in chunks]

    def test_upgrade_is_skipped_above_threshold(self, tmp_path, caplog):
        """超过阈值不自动迁移（重建期间内存翻倍），只告警——不静默。"""
        legacy = _store(tmp_path, index_type="flat")
        legacy.add([_chunk("c1")], [[1.0] + [0.0] * 7])
        legacy.save()

        reloaded = _store(tmp_path)
        reloaded._AUTO_MIGRATE_LIMIT = 0
        reloaded.load()

        import faiss

        assert isinstance(reloaded._index, faiss.IndexFlatIP)
        assert "超过自动迁移阈值" in caplog.text

    def test_empty_legacy_index_upgrades_without_work(self, tmp_path):
        import faiss

        empty = _store(tmp_path, index_type="flat")
        empty.save()

        reloaded = _store(tmp_path)
        reloaded.load()

        assert isinstance(reloaded._index, faiss.IndexHNSWFlat)


class TestHnswRecallMatchesFlat:
    def test_top1_agrees_with_flat_on_small_corpus(self, tmp_path):
        """小库（200 条）上 HNSW 与 FLAT 的 top1 必须一致。

        这条不是「证明 HNSW 无损」——它是证明"打开默认值不会立刻改变检索结果"，
        否则换索引的行为变化会被误读成检索逻辑坏了。
        """
        rng = np.random.default_rng(7)
        vectors = rng.normal(size=(200, 8)).astype("float32")
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        chunks = [_chunk(f"c{i}") for i in range(200)]

        flat = _store(tmp_path / "flat", index_type="flat")
        flat.add(chunks, vectors.tolist())
        hnsw = _store(tmp_path / "hnsw")
        hnsw.add(chunks, vectors.tolist())

        queries = rng.normal(size=(10, 8)).astype("float32")
        queries /= np.linalg.norm(queries, axis=1, keepdims=True)

        for q in queries:
            flat_ids = [c.chunk_id for c, _ in flat.search(q.tolist(), top_k=5, tenant_id="t1")]
            hnsw_ids = [c.chunk_id for c, _ in hnsw.search(q.tolist(), top_k=5, tenant_id="t1")]
            assert flat_ids[0] == hnsw_ids[0]
            assert set(flat_ids) == set(hnsw_ids)


class TestConfig:
    def test_settings_are_validated(self, monkeypatch, tmp_path):
        """配置项必须被启动校验覆盖，否则拼错的东西会一路带到运行时。"""
        from app.core.config import Settings

        s = Settings(_env_file=None)
        assert s.vector_index_type == "hnsw"
        assert s.vector_index_m >= 4
        assert s.vector_index_ef_search >= 1
