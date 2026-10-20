"""知识库服务：上传 → 解析（Router）→ 切块（策略工厂）→ Embedding → 索引。

 重构：
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
from app.services.vectorstore import chunk_version, is_current_version, with_version

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
        self._post_index = None  # 入库后置钩子（），由 main.py 装配
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

    @property
    def default_tenant_id(self) -> str:
        return self._settings.default_tenant_id

    def version_of(self, doc_id: str) -> int:
        """文档的当前索引版本号（召回侧版本过滤的依据， / ）。

        与 chunks_of 的预览过滤同源：reparse 后旧块改名 @v{旧} 保留供溯源，
        召回时只应返回版本号 == 当前版本的块。未收录的文档按 1 处理。
        """
        record = self._docs.get(doc_id)
        return record.version if record else 1

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

    def list_files(self, tenant_id: str | None = None) -> list[DocumentRecord]:
        """文档列表。tenant_id 给定时只返回该租户文档（管理面列表用，对照 KQ-02）。

        ACL 判定（visible_doc_ids）需要全量集合做逐文档判定，故不传 tenant_id 时返回全部。
        """
        docs = self._docs.values()
        if tenant_id is not None:
            docs = [d for d in docs if d.tenant_id == tenant_id]
        return sorted(docs, key=lambda d: d.uploaded_at, reverse=True)

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
        """文档**当前版本**的块（预览页用）。

        历史版本块仍留在向量库里服务引用溯源，但不出现在预览/检索里——
        预览的语义是"现在被索引了什么"，混入历史块会让用户以为切块坏了。
        """
        record = self._docs.get(doc_id)
        current = record.version if record else 1
        chunks = [c for c in self._store.all_chunks() if c.doc_id == doc_id]
        visible = [c for c in chunks if is_current_version(c.chunk_id, doc_id, current)]
        # 存量数据没有版本标记（全为 0），此时不过滤，避免升级后预览突然空白
        return visible or chunks

    def chunks_of_version(self, doc_id: str, version: int) -> list[Chunk]:
        """指定版本的块（历史溯源用；版本号 0 表示无版本标记的存量数据）。"""
        return [
            c for c in self._store.all_chunks()
            if c.doc_id == doc_id and chunk_version(c.chunk_id) == version
        ]

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

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(block)
        return h.hexdigest()

    def create_record(
        self,
        filename: str,
        content: bytes | None = None,
        source_path: Path | None = None,
        tenant_id: str | None = None,
        relocate: bool = False,
    ) -> DocumentRecord:
        """创建文档记录（简单路径与任务路径共用）。

        租户纪律（对照分析文档 KQ-02）：tenant_id 必须由调用方（认证主体）传入，
        缺省回落 default_tenant_id 只用于无主体的系统内部调用。
        content 为空时按 source_path 流式计算哈希（配合流式上传，不把整文件读进内存）。
        """
        settings = self._settings
        if content is not None:
            content_hash = hashlib.sha256(content).hexdigest()
        else:
            if source_path is None:
                raise ApiError(ErrCode.INVALID_INPUT, "content 与 source_path 不能同时为空")
            content_hash = self._hash_file(Path(source_path))
        doc_id = self._resolve_doc_id(Path(filename).stem, content_hash)
        if source_path is None:
            settings.upload_dir.mkdir(parents=True, exist_ok=True)
            source_path = settings.upload_dir / f"{doc_id}{Path(filename).suffix.lower()}"
            source_path.write_bytes(content)
        elif relocate:
            # 流式上传的临时文件改名到规范位置（doc_id 依赖哈希，必须先算完才能定名）
            settings.upload_dir.mkdir(parents=True, exist_ok=True)
            target = settings.upload_dir / f"{doc_id}{Path(filename).suffix.lower()}"
            Path(source_path).replace(target)
            source_path = target
        record = DocumentRecord(
            doc_id=doc_id,
            file_id=uuid.uuid4().hex[:12],
            filename=filename,
            tenant_id=tenant_id or settings.default_tenant_id,
            source_path=str(source_path),
            content_hash=content_hash,
            status=DOC_STATUS_PARSING,
            size=len(content) if content is not None else Path(source_path).stat().st_size,
            uploaded_at=datetime.now(timezone.utc).isoformat(),
        )
        self._docs[doc_id] = record
        self._save_metadata()
        return record

    # ---- 入库：解析 ----

    def parse_document(
        self,
        record: DocumentRecord,
        parse_policy: dict | None = None,
        progress=None,
    ) -> ParseResult:
        """解析。`progress(done, total)` 由解析器调用以推进阶段内进度。

        进度回调是可选参数：不传时解析器内部全部退化为无进度上报，
        因此既有调用方（单文件同步入库、测试替身）不受影响。
        """
        return self._router.parse(
            Path(record.source_path), record.doc_id, record.tenant_id,
            parse_policy or {}, progress=progress,
        )

    # ---- 入库：切块 ----

    def apply_chunking(
        self,
        record: DocumentRecord,
        parsed: ParseResult,
        chunk_policy: dict | None = None,
    ):
        """只做切块（含表格单独成块），把结果挂到 `_pending_chunks` 上等索引阶段取用。

        切块与索引拆成两步，是为了让任务视图能给出真实的"切块中"阶段——
        合并成一步时，这一步里 90% 的时间其实花在嵌入调用上。
        """
        policy = chunk_policy or DEFAULT_POLICY
        settings = self._settings
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
        chunks = list(result.chunks)
        # 表格块：正文即表格 Markdown，modality=table
        seq = len(chunks)
        for source, markdown in table_segments:
            seq += 1
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
        object.__setattr__(record, "_pending_chunks", chunks)
        object.__setattr__(record, "_pending_parents", result.parents)
        object.__setattr__(record, "_pending_chunker", result.chunker)
        object.__setattr__(record, "_pending_parsed", parsed)
        return chunks

    # ---- 入库：索引 ----

    def index_parsed(
        self,
        record: DocumentRecord,
        parsed: ParseResult,
        chunk_policy: dict | None = None,
        progress=None,
    ) -> None:
        """切块 → Embedding → 向量库 / BM25 / DocStore。索引临界区：不可中途取消。

        未先调 `apply_chunking` 时本方法内部自行切块（保持既有调用方兼容）。
        """
        chunks = getattr(record, "_pending_chunks", None)
        parents = getattr(record, "_pending_parents", None)
        chunker_name = getattr(record, "_pending_chunker", "recursive")
        if not chunks:
            chunks = self.apply_chunking(record, parsed, chunk_policy)
            parents = getattr(record, "_pending_parents", None)
            chunker_name = getattr(record, "_pending_chunker", "recursive")

        with self._lock:
            # 写序纪律（对照分析文档 KQ-08）：先算好新向量再清旧索引。
            # 先 purge 后 embed 的顺序下，Embedding 失败 = 旧索引已删、文档从检索消失。
            try:
                vectors = self._embeddings.embed_texts([c.content for c in chunks])
            except Exception as exc:
                raise ApiError(
                    ErrCode.INDEX_BUILD_FAILED, f"索引构建中断（Embedding 失败）: {exc}"
                ) from exc
            # 索引质量红线：向量条数必须严格等于块条数，不等即拒绝发布。
            # 少了这条断言，"块与向量不一致"会表现为检索静默漏召回，而不是报错。
            if len(vectors) != len(chunks):
                raise ApiError(
                    ErrCode.INDEX_BUILD_FAILED,
                    f"索引发布中止：向量条数 {len(vectors)} 与块条数 {len(chunks)} 不一致",
                )
            if not getattr(record, "_skip_purge", False):
                self._purge_chunks(record.doc_id)
            self._store.add(chunks, vectors)
            self._store.save()
            if self._bm25 is not None:
                self._bm25.add(chunks)
            if parents:
                self._docstore.put_parents(parents)
            if parsed.tables:
                self._docstore.put_tables(parsed.tables)
            if parsed.assets:
                # 跨文档去重：同一张图只入一次。此前方法实现了却零调用。
                self._docstore.put_assets(
                    parsed.assets, dedupe_by_hash=self._store.total > 0
                )
            if progress:
                progress(len(chunks), len(chunks))

            record.chunk_count = len(chunks)
            record.short_chunk_count = sum(1 for c in chunks if c.is_short)
            record.status = DOC_STATUS_INDEXED
            record.error = None
            record.outline = parsed.outline
            record.chunker = chunker_name
            self._bump_version()
            self._save_metadata()
            logger.info(
                "文档入库完成: %s（%d 块, 表格=%d, 资源=%d, chunker=%s, 超短块=%d）",
                record.doc_id, len(chunks), len(parsed.tables), len(parsed.assets),
                chunker_name, record.short_chunk_count,
            )
        for name in (
            "_pending_chunks", "_pending_parents", "_pending_chunker",
            "_pending_parsed", "_skip_purge",
        ):
            if hasattr(record, name):
                object.__delattr__(record, name)

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
        tenant_id: str | None = None,
    ) -> DocumentRecord:
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ApiError(ErrCode.UNSUPPORTED_FILE, f"仅支持 {sorted(SUPPORTED_EXTENSIONS)} 文件")
        if len(content) > self._settings.max_upload_mb * 1024 * 1024:
            raise ApiError(ErrCode.FILE_TOO_LARGE, f"文件超过 {self._settings.max_upload_mb}MB 限制")

        record = self.create_record(filename, content, tenant_id=tenant_id)
        return self._index_record(record, chunk_policy, parse_policy)

    def ingest_path(
        self,
        filename: str,
        source_path: Path,
        chunk_policy: dict | None = None,
        parse_policy: dict | None = None,
        tenant_id: str | None = None,
    ) -> DocumentRecord:
        """流式上传路径：文件已在磁盘上（边写边校验过大小），直接建档入库。"""
        ext = Path(filename).suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            raise ApiError(ErrCode.UNSUPPORTED_FILE, f"仅支持 {sorted(SUPPORTED_EXTENSIONS)} 文件")
        record = self.create_record(
            filename, source_path=source_path, tenant_id=tenant_id, relocate=True
        )
        return self._index_record(record, chunk_policy, parse_policy)

    def _index_record(
        self,
        record: DocumentRecord,
        chunk_policy: dict | None,
        parse_policy: dict | None,
    ) -> DocumentRecord:
        try:
            parsed = self.parse_document(record, parse_policy)
            self.index_parsed(record, parsed, chunk_policy)
            self._run_post_index(record, parsed)
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

    def set_post_index(self, hook) -> None:
        """注册入库后置钩子（图谱构建 + 图像索引，）。

        同步上传（`POST /knowledge/upload`）与异步任务（`TaskManager`）是两条
        入库路径，钩子必须挂在服务层，两条路径才会一致生效——只挂一边的话，
        "上传完去图谱页看看"在另一条路径上永远是空的。
        """
        self._post_index = hook

    def _run_post_index(self, record, parsed) -> None:
        """执行后置钩子。失败只记日志：图谱是增强层，不能让文档入库跟着失败。"""
        if self._post_index is None:
            return
        try:
            stats = self._post_index(record, parsed)
            if stats:
                logger.info("图谱/图像索引联动完成 %s: %s", record.doc_id, stats)
        except Exception as exc:  # noqa: BLE001
            logger.warning("图谱/图像索引联动失败（不影响入库）: %s", exc)

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
        """用新策略重新解析：**生成新版本块集合，旧版本下线但保留块**（）。

        旧实现先物理删除旧块再重建，且 chunk_id 复用同一序号空间——结果���所有历史答案
        里的引用编号全部指向不存在的块，溯源链在"重新解析"这个动作上静默断裂。

        现在的做法：
        1. 旧块的 chunk_id 加 `@v{旧版本}` 后缀落到"历史版本"区，**不删除**；
        2. 新版本块的 chunk_id 带 `@v{新版本}`，与旧版本天然不冲突；
        3. 检索侧按 `is_short`/doc_id 过滤，历史块不参与召回，只服务溯源；
        4. 保留最近 `index_version_keep` 个版本，更早的才真删。
        """
        record = self.get(doc_id)
        record.status = DOC_STATUS_PARSING
        record.error = None
        self._save_metadata()
        try:
            parsed = self.parse_document(record, parse_policy)
            self.apply_chunking(record, parsed, chunk_policy)
            old_version = record.version or 1
            new_version = old_version + 1
            chunks = getattr(record, "_pending_chunks", [])
            # 先把当前版本的块改名为历史版本（保留供溯源），再给新块打新版本号
            self._retire_version(record.doc_id, old_version)
            for chunk in chunks:
                chunk.chunk_id = with_version(chunk.chunk_id, new_version)
            object.__setattr__(record, "_pending_chunks", chunks)
            record.version = new_version
            # reparse 不再走 _purge_chunks：旧块已改名下线，新块直接叠加
            object.__setattr__(record, "_skip_purge", True)
            self.index_parsed(record, parsed, chunk_policy)
        except ApiError as exc:
            record.status = DOC_STATUS_FAILED
            record.error = exc.message
            self._save_metadata()
        return record

    def _retire_version(self, doc_id: str, old_version: int) -> int:
        """把当前版本的块改名为"历史版本"并从检索索引中下线（**不删除**）。

        历史答案里的引用编号靠 chunk_id 回溯，删掉就再也对不上原文了。改名后：
        - 与新版本的序号空间隔离，不会撞号；
        - 仍可在 `/knowledge/preview/.../chunk/{chunk_id}` 中被直接查到；
        - 超出 `index_version_keep` 份数的更早版本才真正删除。
        """
        renamed = 0
        if hasattr(self._store, "rename_doc_version"):
            renamed = len(self._store.rename_doc_version(doc_id, old_version))
        else:
            logger.warning("向量库不支持 rename_doc_version，历史块可能被覆盖: %s", doc_id)
        keep = max(1, self._settings.index_version_keep)
        removed = 0
        if hasattr(self._store, "delete_doc_versions"):
            removed = self._store.delete_doc_versions(doc_id, keep_versions=keep)
        self._bump_version()
        logger.info(
            "文档 %s 索引版本 → v%d（历史块改名 %d 条保留供溯源，超保留份数删除 %d 条）",
            doc_id, old_version, renamed, removed,
        )
        return renamed

    def offline(self, doc_id: str) -> DocumentRecord:
        """删除索引（临时下架）：块从检索消失，源文件与记录保留。"""
        record = self.get(doc_id)
        with self._lock:
            self._purge_chunks(doc_id)
        record.chunk_count = 0
        record.status = DOC_STATUS_OFFLINE
        self._save_metadata()
        return record

    # ---- 删除编排（对照分析文档 KQ-08：派生副本必须随主数据一并收敛）----

    def set_deletion_deps(
        self, acl=None, graph_store=None, image_index=None, task_manager=None
    ) -> None:
        """装配期注入删除编排的协作者（main.py 在各组件就绪后回填）。"""
        self._del_acl = acl
        self._del_graph_store = graph_store
        self._del_image_index = image_index
        self._del_task_manager = task_manager

    def delete(self, doc_id: str) -> DocumentRecord:
        """删除文档：索引、派生副本（图谱/图像/资产/ACL/任务文件）与源文件一并删除。

        派生副本按 best-effort 逐项清理：单项失败不中断整体删除，
        失败的项写进 warning 日志（运维可据此做残留巡检）。
        """
        record = self.get(doc_id)
        # 先取清理所需的引用（purge 之后就拿不到了）
        try:
            chunk_ids = set(self._store.doc_chunk_ids(doc_id))
        except Exception:  # noqa: BLE001
            chunk_ids = set()
        try:
            assets = list(self._docstore.assets_of_doc(doc_id))
        except Exception:  # noqa: BLE001
            assets = []
        with self._lock:
            self._purge_chunks(doc_id)
        self._docs.pop(doc_id, None)

        residuals: list[str] = []
        acl = getattr(self, "_del_acl", None)
        if acl is not None:
            try:
                acl.remove(doc_id)
            except Exception as exc:  # noqa: BLE001
                residuals.append(f"acl: {exc}")
        graph_store = getattr(self, "_del_graph_store", None)
        if graph_store is not None and chunk_ids:
            try:
                graph_store.delete_doc_chunks(chunk_ids)
            except Exception as exc:  # noqa: BLE001
                residuals.append(f"graph: {exc}")
        image_index = getattr(self, "_del_image_index", None)
        if image_index is not None:
            try:
                image_index.remove_doc(doc_id)
            except Exception as exc:  # noqa: BLE001
                residuals.append(f"images: {exc}")
        for asset in assets:
            try:
                path = Path(getattr(asset, "path", "") or "")
                if path.exists():
                    path.unlink()
            except Exception as exc:  # noqa: BLE001
                residuals.append(f"asset: {exc}")
        task_manager = getattr(self, "_del_task_manager", None)
        if task_manager is not None:
            try:
                task_manager.remove_files_for_doc(doc_id)
            except Exception as exc:  # noqa: BLE001
                residuals.append(f"tasks: {exc}")

        source = Path(record.source_path)
        if source.exists():
            source.unlink()
        self._save_metadata()
        if residuals:
            logger.warning("文档 %s 已删除，但存在未清理残留: %s", doc_id, residuals)
        return record
