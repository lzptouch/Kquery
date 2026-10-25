"""F4 快问单测：隔离性、双写、版本不 bump、缓存键与过滤维度。

这些断言对应的是**"实现但无效"**的四类事故：
实现了快问但污染了知识库、只写了向量库导致关键词零召回、
误 bump 版本导致全站缓存被清空、缓存键少了 scope 导致跨文档串答案。
"""
import asyncio
import threading
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import Chunk, DocumentRecord
from app.services.cache import make_cache_key
from app.services.knowledge import KnowledgeService
from app.services.parsers.base import ParseResult
from app.services.pipeline import RagPipeline
from app.services.quickask import InMemoryQuickAskStore, QuickAskService
from app.services.security.auth import Principal


# ---------- 替身 ----------

class _StubEmbeddings:
    def embed_texts(self, texts):
        return [[0.1] * 8 for _ in texts]


class _StubStore:
    def __init__(self):
        self.chunks: list[Chunk] = []
        self.saved = 0

    def add(self, chunks, vectors):
        self.chunks.extend(chunks)

    def save(self):
        self.saved += 1

    def doc_chunk_ids(self, doc_id):
        return [c.chunk_id for c in self.chunks if c.doc_id == doc_id]

    def delete_doc(self, doc_id):
        self.chunks = [c for c in self.chunks if c.doc_id != doc_id]


class _StubBM25:
    def __init__(self):
        self.added: list[Chunk] = []
        self.deleted: list[str] = []

    def add(self, chunks):
        self.added.extend(chunks)

    def delete_doc(self, doc_id):
        self.deleted.append(doc_id)


class _StubDocStore:
    def __init__(self):
        self.deleted: list[str] = []
        self.parents_written = 0
        self.tables_written = 0

    def delete_doc(self, doc_id):
        self.deleted.append(doc_id)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        model_provider="mock",
        auth_enabled=False,
        quick_ask_ttl_s=60,
        quick_ask_max_pages=5,
        quick_ask_max_mb=1,
    )


def _knowledge(settings: Settings, store, bm25, docstore) -> KnowledgeService:
    service = KnowledgeService(
        settings,
        _StubEmbeddings(),
        None,
        store,
        bm25,
        docstore,
        None,  # parser_router：快问单测里不走真实解析
    )
    return service


def _record(doc_id: str = "quick_test") -> DocumentRecord:
    return DocumentRecord(
        doc_id=doc_id,
        file_id=doc_id,
        filename="a.md",
        tenant_id="default",
        source_path="/tmp/a.md",
        content_hash="hash",
        size=10,
    )


def _chunk(doc_id: str, seq: int = 1) -> Chunk:
    return Chunk(
        chunk_id=f"{doc_id}#{seq}", doc_id=doc_id, seq=seq,
        content="快问内容片段，足够长以通过最小长度检查", tenant_id="default",
        source="全文",
    )


def _principal(user_id: str = "u1") -> Principal:
    return Principal(user_id=user_id, tenant_id="default", roles=[])


def _run(coro):
    """在独立线程里跑协程。

    直接用 `asyncio.run` 会在"当前线程已有事件循环"时抛
    RuntimeError——测试套件里别的用例（如 Agent 相关）可能留下一个未关闭的
    事件循环，导致本用例莫名其妙地失败。独立线程自带干净的事件循环。
    """
    box: dict = {}

    def _target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - 原样抛回调用线程
            box["error"] = exc

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


# ---------- 隔离性 ----------

class TestQuickAskIsolation:
    def test_upload_does_not_touch_metadata(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store, bm25, docstore = _StubStore(), _StubBM25(), _StubDocStore()
        knowledge = _knowledge(settings, store, bm25, docstore)

        record = _record()
        object.__setattr__(record, "_pending_chunks", [_chunk(record.doc_id)])
        n = knowledge.index_transient(record, ParseResult(segments=[("全文", "内容")]))

        assert n == 1
        assert knowledge._docs == {}, "快问不得进入 _docs（否则会出现在文件列表）"
        assert not settings.metadata_file.exists(), "快问不得写 metadata.json"
        assert docstore.deleted == [] and docstore.parents_written == 0, "快问不得写 DocStore"

    def test_writes_vector_and_bm25(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store, bm25, docstore = _StubStore(), _StubBM25(), _StubDocStore()
        knowledge = _knowledge(settings, store, bm25, docstore)

        record = _record()
        object.__setattr__(record, "_pending_chunks", [_chunk(record.doc_id)])
        knowledge.index_transient(record, ParseResult(segments=[("全文", "内容")]))

        assert len(store.chunks) == 1, "必须写向量库"
        assert len(bm25.added) == 1, "必须写 BM25（只写向量库会让关键词路零召回）"

    def test_purge_transient_no_version_bump(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store, bm25, docstore = _StubStore(), _StubBM25(), _StubDocStore()
        knowledge = _knowledge(settings, store, bm25, docstore)

        record = _record()
        object.__setattr__(record, "_pending_chunks", [_chunk(record.doc_id)])
        knowledge.index_transient(record, ParseResult(segments=[("全文", "内容")]))
        before = knowledge.data_version

        knowledge.purge_transient(record.doc_id)

        assert knowledge.data_version == before, (
            "快问清理不得 bump 索引版本（版本进缓存键，bump 会清空全站语义缓存）"
        )
        assert store.chunks == [] and bm25.deleted == [record.doc_id]
        assert docstore.deleted == [], "快问从未写 DocStore，清理也不该动它"


# ---------- 缓存键与过滤维度 ----------

def _pipeline(settings: Settings) -> RagPipeline:
    pipeline = object.__new__(RagPipeline)
    pipeline._settings = settings
    pipeline._index_version = lambda: 1
    pipeline._acl_version = lambda: 0
    pipeline._crag = None
    pipeline._user_id = "u1"
    pipeline._acl_filter = None
    pipeline._visible_docs_provider = None
    return pipeline


class TestQuickAskCacheAndFilters:
    def test_cache_key_includes_doc_id(self, tmp_path: Path):
        settings = _settings(tmp_path)
        pipeline = _pipeline(settings)
        config = _retrieval_config()

        k1 = pipeline._cache_key("这个文件讲了什么", "default", config, "quick_a")
        k2 = pipeline._cache_key("这个文件讲了什么", "default", config, "quick_b")
        k3 = pipeline._cache_key("这个文件讲了什么", "default", config, None)

        assert k1 != k2, "不同文档的缓存键必须不同（否则跨文档串答案）"
        assert k1 != k3, "快问与全库检索的缓存键必须不同"
        assert k1 == pipeline._cache_key("这个文件讲了什么", "default", config, "quick_a")

    def test_make_cache_key_scope_dimension(self):
        base = make_cache_key("t", "q", "m", "c")
        scoped = make_cache_key("t", "q", "m", "c", scope="quick_a")
        assert base != scoped

    def test_filters_built_when_acl_disabled(self, tmp_path: Path):
        """ACL 关闭时（默认部署与测试环境）doc_id 过滤必须仍然生效。"""
        settings = _settings(tmp_path)
        pipeline = _pipeline(settings)

        filters = pipeline._build_filters("default", "quick_a")
        assert filters is not None, "ACL 关闭时也要为快问构造过滤条件"
        assert filters.doc_id == "quick_a"
        assert filters.allows("quick_a", "default") is True
        assert filters.allows("other_doc", "default") is False

    def test_filters_none_without_doc_id(self, tmp_path: Path):
        settings = _settings(tmp_path)
        pipeline = _pipeline(settings)
        assert pipeline._build_filters("default", None) is None


def _retrieval_config():
    from app.domain.schemas import RetrievalConfig

    return RetrievalConfig()


# ---------- 服务层 ----------

class TestQuickAskService:
    def test_upload_registers_meta(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        settings = _settings(tmp_path)
        store, bm25, docstore = _StubStore(), _StubBM25(), _StubDocStore()
        knowledge = _knowledge(settings, store, bm25, docstore)
        service = QuickAskService(settings, knowledge)
        original_index = knowledge.index_transient

        def _fake_parse(record, parse_policy=None, progress=None):
            return ParseResult(segments=[("全文", "内容")])

        def _fake_index(record, parsed, chunk_policy=None):
            object.__setattr__(record, "_pending_chunks", [_chunk(record.doc_id)])
            return original_index(record, parsed)

        monkeypatch.setattr(knowledge, "parse_document", _fake_parse)
        monkeypatch.setattr(knowledge, "index_transient", _fake_index)

        info = _run(service.upload("a.md", b"# hello", _principal()))
        assert info["doc_id"].startswith("quick_")
        assert info["chunks"] == 1
        assert service.get_meta(info["doc_id"], _principal())["filename"] == "a.md"

    def test_foreign_user_cannot_access(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        settings = _settings(tmp_path)
        store, bm25, docstore = _StubStore(), _StubBM25(), _StubDocStore()
        knowledge = _knowledge(settings, store, bm25, docstore)
        service = QuickAskService(settings, knowledge)
        original_index = knowledge.index_transient

        monkeypatch.setattr(
            knowledge, "parse_document",
            lambda record, parse_policy=None, progress=None: ParseResult(segments=[("全文", "x")]),
        )
        monkeypatch.setattr(
            knowledge, "index_transient",
            lambda record, parsed, chunk_policy=None: _index_with_stub_chunk(original_index, record, parsed),
        )

        info = _run(service.upload("a.md", b"# hello", _principal("u1")))
        with pytest.raises(ApiError) as exc:
            service.get_meta(info["doc_id"], _principal("u2"))
        assert exc.value.code == ErrCode.QUICK_DOC_EXPIRED
        assert exc.value.http_status == 404

    def test_expired_doc_rejected(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store, bm25, docstore = _StubStore(), _StubBM25(), _StubDocStore()
        knowledge = _knowledge(settings, store, bm25, docstore)
        service = QuickAskService(settings, knowledge)
        with pytest.raises(ApiError) as exc:
            service.get_meta("quick_missing", _principal())
        assert exc.value.code == ErrCode.QUICK_DOC_EXPIRED

    def test_unsupported_type_rejected(self, tmp_path: Path):
        settings = _settings(tmp_path)
        store, bm25, docstore = _StubStore(), _StubBM25(), _StubDocStore()
        knowledge = _knowledge(settings, store, bm25, docstore)
        service = QuickAskService(settings, knowledge)
        with pytest.raises(ApiError) as exc:
            _run(service.upload("a.bin", b"\x00", _principal()))
        assert exc.value.code == ErrCode.UNSUPPORTED_FILE_TYPE

    def test_in_memory_store_expires(self):
        store = InMemoryQuickAskStore()
        store.put("quick_a", {"x": 1}, ttl_s=0)
        # ttl=0 → 立即过期
        assert store.get("quick_a") is None


def _index_with_stub_chunk(index, record, parsed) -> int:
    object.__setattr__(record, "_pending_chunks", [_chunk(record.doc_id)])
    return index(record, parsed)
