"""BM25 的索引版本生命周期（`X-14`）。

## 背景：为什么 BM25 必须跟着向量库改

`reparse` 的语义是「旧块改名成历史版本、新块进入当前版本」。此前只有向量库
执行改名与旧版本清理，BM25 完全不知道这回事，于是：

① 关键词路仍持有**无版本号**的旧块 id，而向量库/文档存储里已经是 `@v2#0`。
   关键词命中的 id 查不到 → 用户点开引用是死链；
② 已下线版本的块仍会被关键词召回（过期内容混进答案证据）；
③ 旧块永不释放，内存随 reparse 次数线性增长。

这三条都不会让任何"功能测试"变红——只有专门盯索引生命周期的用例能发现。
"""
from app.domain.schemas import Chunk
from app.services.retrieval.bm25 import BM25Retriever


def _chunk(chunk_id: str, doc_id: str = "d1", content: str = "电池电压过高", seq: int = 0) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=doc_id,
        seq=seq,
        content=content,
        tenant_id="t1",
        source="p1",
    )


def test_rename_doc_version_rewrites_plain_ids():
    bm25 = BM25Retriever()
    bm25.add([_chunk("d1#0"), _chunk("d1#1", seq=1)])
    renamed = bm25.rename_doc_version("d1", 3)
    assert renamed == 2
    # 与向量库 `with_version` 完全一致：{doc}@v{n}#{seq}
    assert sorted(bm25._chunks) == ["d1@v3#0", "d1@v3#1"]
    # 改名后仍可被关键词检索命中（只是 id 变了）
    hits = bm25.search("电池", top_n=5, tenant_id="t1")
    assert {h.chunk.chunk_id for h in hits} == {"d1@v3#0", "d1@v3#1"}


def test_rename_is_idempotent_for_versioned_chunks():
    bm25 = BM25Retriever()
    bm25.add([_chunk("d1@v2#0")])
    # 已经带版本号的块不再二次改名（否则会出现 @v3@v2 之类的叠加）
    assert bm25.rename_doc_version("d1", 9) == 0
    assert list(bm25._chunks) == ["d1@v2#0"]


def test_rename_does_not_touch_other_docs():
    bm25 = BM25Retriever()
    bm25.add([_chunk("d1#0"), _chunk("d2#0", doc_id="d2")])
    bm25.rename_doc_version("d1", 2)
    assert sorted(bm25._chunks) == ["d1@v2#0", "d2#0"]


def test_delete_doc_versions_keeps_latest_and_frees_memory():
    bm25 = BM25Retriever()
    bm25.add([
        _chunk("d1@v1#0"),
        _chunk("d1@v2#0"),
        _chunk("d1@v3#0"),
        _chunk("d1@v4#0"),
    ])
    removed = bm25.delete_doc_versions("d1", keep_versions=2)
    assert removed == 2
    # 保留最近两版，旧版本不再占用内存
    assert sorted(bm25._chunks) == ["d1@v3#0", "d1@v4#0"]
    assert len(bm25._order) == 2


def test_delete_doc_versions_noop_below_limit():
    bm25 = BM25Retriever()
    bm25.add([_chunk("d1@v1#0"), _chunk("d1@v2#0")])
    assert bm25.delete_doc_versions("d1", keep_versions=3) == 0
    assert len(bm25._chunks) == 2


def test_delete_doc_versions_leaves_other_docs_alone():
    bm25 = BM25Retriever()
    bm25.add([
        _chunk("d1@v1#0"),
        _chunk("d1@v2#0"),
        _chunk("d2@v1#0", doc_id="d2"),
    ])
    bm25.delete_doc_versions("d1", keep_versions=1)
    assert sorted(bm25._chunks) == ["d1@v2#0", "d2@v1#0"]


def test_plain_chunks_are_not_counted_as_a_version():
    """存量数据（无 `@v`）不能算作一个历史版本，否则第一次 reparse 就会误删。"""
    bm25 = BM25Retriever()
    bm25.add([_chunk("d1#0")])
    assert bm25.delete_doc_versions("d1", keep_versions=1) == 0
    assert list(bm25._chunks) == ["d1#0"]
