"""M2：chunk 级删除（文档删除后图像描述不得残留）。"""
import tempfile
from pathlib import Path

from app.domain.schemas import Chunk


def _chunk(cid: str, doc_id: str) -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=doc_id,
        seq=1,
        content=f"内容 {cid}",
        source="测试",
        tenant_id="t1",
    )


class TestVectorStoreDeleteChunks:
    def _store(self):
        from app.services.vectorstore import VectorStore

        store = VectorStore(Path(tempfile.mkdtemp()), dim=8)
        chunks = [
            _chunk("doc#1", "doc"),
            _chunk("__images__#a1", "__images__"),
            _chunk("__images__#a2", "__images__"),
        ]
        store.add(chunks, [[0.1] * 8 for _ in chunks])
        return store

    def test_delete_chunks_removes_only_given_ids(self):
        store = self._store()
        assert store.total == 3
        removed = store.delete_chunks({"__images__#a1"})
        assert removed == 1
        assert store.total == 2
        assert store.doc_chunk_ids("__images__") == ["__images__#a2"]

    def test_delete_doc_does_not_touch_shared_image_doc(self):
        """`delete_doc(doc)` 碰不到共享 doc_id 下的图像块 —— 正是需要 chunk 级删除的原因。"""
        store = self._store()
        store.delete_doc("doc")
        assert sorted(store.doc_chunk_ids("__images__")) == ["__images__#a1", "__images__#a2"]

    def test_delete_empty_set_is_noop(self):
        store = self._store()
        assert store.delete_chunks(set()) == 0
        assert store.total == 3


class TestImageIndexChunkIds:
    def test_chunk_ids_of_lists_image_chunks(self):
        """图像条目 → 它们在向量库里占用的 chunk_id（M2 的删除清单）。"""
        from app.services.graph.images import IMAGE_DOC_ID, ImageIndex

        class _StubIndex(ImageIndex):
            def __init__(self):  # 绕开 embeddings / vector_store 的装配
                self._entries = {}

        index = _StubIndex()
        assert index.chunk_ids_of("doc-1") == set()
        assert IMAGE_DOC_ID.startswith("__")
