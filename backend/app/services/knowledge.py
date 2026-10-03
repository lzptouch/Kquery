"""知识库服务：上传 → 解析（Router）→ 切块（策略工厂）→ Embedding → 索引。

阶段 3 重构：
- 解析走 ParserRouter（md / pdf 文字+表格 / OCR / 音频）；
- 切块走策略工厂（recursive / semantic / parent_child / raptor）；
- 表格单独成块（modality=table），不被切块器拦腰切断；
- 父块 / 表格 / 媒体资源入 DocStore，与向量库分开存放；
- 支持"删除索引（下架）"与"重新解析（新策略重建）"两种高级操作。

失败纪律：单文件失败不阻塞其它文件；索引是块与向量一致性的临界区，
嵌入整批中止时保留已完成的向量，不允许半成品索引文件。
"""
import hashlib
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import Settings
from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import (
    DOC_STATUS_FAILED,
    DOC_STATUS_INDEXED,
    DOC_STATUS_OFFLINE,
    DOC_STATUS_PARSING,
    Chunk,
    DocumentRecord,
)
from app.services.chunkers.factory import DEFAULT_POLICY, apply_chunk_policy
from app.services.docstore import DocStore
from app.services.embeddings import Embeddings
from app.services.llm import LLM
from app.services.parsers.base import ParseResult
from app.services.parsers.router import SUPPORTED_EXTENSIONS, ParserRouter
from app.services.retrieval.bm25 import BM25Retriever

logger = logging.getLogger(__name__)

TABLE_SOURCE_MARK = "·表格"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^\w一-鿿-]+", "-", name).strip("-").lower()
    return slug or "doc"


class KnowledgeService:
    def __init__(
        self,
        settings: Settings,
        embeddings: Embeddings,
        llm: LLM,
        store,
        bm25: BM25Retriever | None,
        docstore: DocStore,
        parser_router: ParserRouter,
    ):
        self._settings = settings
        self._embeddings = embeddings
        self._llm = llm
        self._store = store
        self._bm25 = bm25
        self._docstore = docstore
        self._router = parser_router
        self._lock = threading.Lock()
        self._docs: dict[str, DocumentRecord] = {}
        self._version_file = settings.data_dir / "index_version"
        self._data_version = self._read_version()
        self._load_metadata()

    # ---- 索引版本（缓存一致性：知识库任何变更 → 版本+1 → 旧缓存键自然失效）----

    def _read_version(self) -> int:
        try:
            return int(self._version_file.read_text().strip())
        except Exception:
            return 0

    def _bump_version(self) -> None:
        self._data_version += 1
        self._version_file.parent.mkdir(parents=True, exist_ok=True)
        self._version_file.write_text(str(self._data_version))

    @property
    def data_version(self) -> int:
        return self._data_version

    # ---- 元数据持久化 ----

    def _load_metadata(self) -> None:
        path = self._settings.metadata_file
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._docs = {d["doc_id"]: DocumentRecord.model_validate(d) for d in raw}
        except Exception as exc:
            raise RuntimeError(f"文档元数据文件损坏，拒绝启动: {exc}") from exc

    def _save_metadata(self) -> None:
        path = self._settings.metadata_file
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([d.model_dump() for d in self._docs.values()], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)

    # ---- 查询 ----

    def list_files(self) -> list[DocumentRecord]:
        return sorted(self._docs.values(), key=lambda d: d.uploaded_at, reverse=True)

    def get(self, doc_id: str) -> DocumentRecord:
        doc = self._docs.get(doc_id)
        if doc is None:
            raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, f"文档不存在: {doc_id}", http_status=404)
        return doc

    def filename_of(self, doc_id: str) -> str:
        doc = self._docs.get(doc_id)
        return doc.filename if doc else doc_id

    def outline_of(self, doc_id: str) -> list[dict]:
        """预览页左侧大纲：优先取入库时持久化的大纲；老数据 Markdown 现场重算兜底。"""
        from app.services.parsers.base import extract_md_outline

        record = self.get(doc_id)
        if record.outline:
            return record.outline
        source = Path(record.source_path)
        if source.suffix.lower() in {".md", ".markdown"} and source.exists():
            return extract_md_outline(source.read_text(encoding="utf-8"))
        return []

    def chunks_of(self, doc_id: str) -> list[Chunk]:
        return [c for c in self._store.all_chunks() if c.doc_id == doc_id]

    def tables_of(self, doc_id: str):
        return self._docstore.tables_of_doc(doc_id)

    def parents_of(self, doc_id: str) -> list[Chunk]:
        """父子分块的父块（DocStore，预览页双层列表用）。"""
        return sorted(self._docstore.parents_of_doc(doc_id), key=lambda p: p.seq)

    def assets_of(self, doc_id: str):
        return self._docstore.assets_of_doc(doc_id)

    # ---- 入库：建档 ----

    def _resolve_doc_id(self, stem: str, content_hash: str) -> str:
        base = _slugify(stem)
        existing = self._docs.get(base)
        if existing is None or existing.content_hash == content_hash:
            return base
        return f"{base}-{content_hash[:8]}"

    def create_record(self, filename: str, content: bytes, source_path: Path | None = None) -> DocumentRecord:
        """创建文档记录（简单路径与任务路径共用）。"""
        settings = self._settings
        content_hash = hashlib.sha256(content).hexdigest()
        doc_id = self._resolve_doc_id(Path(filename).stem, content_hash)
        if source_path is None:
            settings.upload_dir.mkdir(parents=True, exist_ok=True)
            source_path = settings.upload_dir / f"{doc_id}{Path(filename).suffix.lower()}"
            source_path.write_bytes(content)
        record = DocumentRecord(
            doc_id=doc_id,
            file_id=uuid.uuid4().hex[:12],
            filename=filename,
            tenant_id=settings.default_tenant_id,
            source_path=str(source_path),
            content_hash=content_hash,
            status=DOC_STATUS_PARSING,
            size=len(content),
            uploaded_at=datetime.now(timezone.utc).isoformat(),
        )
        self._docs[doc_id] = record
        self._save_metadata()
        return record

    # ---- 入库：解析 ----

    def parse_document(self, record: DocumentRecord, parse_policy: dict | None = None) -> ParseResult:
        return self._router.parse(
            Path(record.source_path), record.doc_id, record.tenant_id, parse_policy or {}
        )

    # ---- 入库：切块 + 索引 ----

    def index_parsed(
        self,
        record: DocumentRecord,
        parsed: ParseResult,
        chunk_policy: dict | None = None,
    ) -> None:
        """切块 → Embedding → 向量库 / BM25 / DocStore。索引临界区：不可中途取消。"""
        policy = chunk_policy or DEFAULT_POLICY
        settings = self._settings
        with self._lock:
            self._purge_chunks(record.doc_id)

            # 表格段与文本段分离：表格单独成块，不被切块器切断
            text_segments = [s for s in parsed.segments if TABLE_SOURCE_MARK not in s[0]]
            table_segments = [s for s in parsed.segments if TABLE_SOURCE_MARK in s[0]]

            result = apply_chunk_policy(
                record.doc_id, text_segments, record.tenant_id, policy,
                self._embeddings, self._llm,
                chunk_size=settings.chunk_size,
                chunk_overlap=settings.chunk_overlap,
                min_chars=settings.min_chunk_chars,
            )
            chunks = result.chunks
            # 表格块：正文即表格 Markdown，modality=table
            seq = len(chunks)
            for source, markdown in table_segments:
                seq += 1
                page_info = source.split("·")[0]
                chunks.append(
                    Chunk(
                        chunk_id=f"{record.doc_id}#{seq}",
                        doc_id=record.doc_id,
                        seq=seq,
                        content=markdown,
                        tenant_id=record.tenant_id,
                        source=source,
                        chunker="table",
                        modality="table",
                    )
                )
            if not chunks:
                raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "切块结果为 0 块，请检查文件是否加密或损坏")

            try:
                vectors = self._embeddings.embed_texts([c.content for c in chunks])
            except Exception as exc:
                raise ApiError(
                    ErrCode.INDEX_BUILD_FAILED, f"索引构建中断（Embedding 失败）: {exc}"
                ) from exc
            self._store.add(chunks, vectors)
            self._store.save()
            if self._bm25 is not None:
                self._bm25.add(chunks)
            if result.parents:
                self._docstore.put_parents(result.parents)
            if parsed.tables:
                self._docstore.put_tables(parsed.tables)
            if parsed.assets:
                self._docstore.put_assets(parsed.assets)

            record.chunk_count = len(chunks)
            record.status = DOC_STATUS_INDEXED
            record.error = None
            record.outline = parsed.outline
            self._bump_version()
            self._save_metadata()
            logger.info(
                "文档入库完成: %s（%d 块, 表格=%d, 资源=%d, chunker=%s）",
                record.doc_id, len(chunks), len(parsed.tables), len(parsed.assets), result.chunker,
            )

    def _purge_chunks(self, doc_id: str) -> None:
        if self._store.doc_chunk_ids(doc_id):
            self._store.delete_doc(doc_id)
            self._store.save()
            self._bump_version()
        if self._bm25 is not None:
            self._bm25.delete_doc(doc_id)
        self._docstore.delete_doc(doc_id)

    # ---- 简单路径：上传即入库（同步，默认策略）----

    def ingest_file(
        self,
        filename: str,
        content: bytes,
        chunk_policy: dict | None = None,
        parse_policy: dict | None = None,
    ) -> DocumentRecord:
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ApiError(ErrCode.UNSUPPORTED_FILE, f"仅支持 {sorted(SUPPORTED_EXTENSIONS)} 文件")
        if len(content) > self._settings.max_upload_mb * 1024 * 1024:
            raise ApiError(ErrCode.FILE_TOO_LARGE, f"文件超过 {self._settings.max_upload_mb}MB 限制")

        record = self.create_record(filename, content)
        try:
            parsed = self.parse_document(record, parse_policy)
            self.index_parsed(record, parsed, chunk_policy)
        except ApiError as exc:
            record.status = DOC_STATUS_FAILED
            record.error = exc.message
            self._save_metadata()
            logger.warning("文档入库失败: %s - %s", record.doc_id, exc.message)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不阻塞其它文件
            record.status = DOC_STATUS_FAILED
            record.error = f"索引构建中断: {exc}"
            self._save_metadata()
            logger.exception("文档索引构建中断: %s", record.doc_id)
        return record

    # ---- 高级操作 ----

    def rebuild(self, doc_id: str) -> DocumentRecord:
        """按原内容原策略重建索引（chunk_id 稳定）。"""
        record = self.get(doc_id)
        record.status = DOC_STATUS_PARSING
        record.error = None
        self._save_metadata()
        try:
            parsed = self.parse_document(record)
            self.index_parsed(record, parsed)
        except ApiError as exc:
            record.status = DOC_STATUS_FAILED
            record.error = exc.message
            self._save_metadata()
        return record

    def reparse(self, doc_id: str, chunk_policy: dict, parse_policy: dict | None = None) -> DocumentRecord:
        """用新策略重新解析（生成新版本块集合替换旧索引）。"""
        record = self.get(doc_id)
        record.status = DOC_STATUS_PARSING
        record.error = None
        self._save_metadata()
        try:
            parsed = self.parse_document(record, parse_policy)
            self.index_parsed(record, parsed, chunk_policy)
        except ApiError as exc:
            record.status = DOC_STATUS_FAILED
            record.error = exc.message
            self._save_metadata()
        return record

    def offline(self, doc_id: str) -> DocumentRecord:
        """删除索引（临时下架）：块从检索消失，源文件与记录保留。"""
        record = self.get(doc_id)
        with self._lock:
            self._purge_chunks(doc_id)
        record.chunk_count = 0
        record.status = DOC_STATUS_OFFLINE
        self._save_metadata()
        return record

    def delete(self, doc_id: str) -> DocumentRecord:
        """删除文档：索引与源文件一并删除（不可恢复）。"""
        record = self.get(doc_id)
        with self._lock:
            self._purge_chunks(doc_id)
        self._docs.pop(doc_id, None)
        source = Path(record.source_path)
        if source.exists():
            source.unlink()
        self._save_metadata()
        return record
