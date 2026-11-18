"""存储装配：索引别名 / 向量库 / BM25 / 文档存储 / 解析路由 / 混合检索（`A-12`）。

## 顺序纪律：先建别名指针，再装存储

物理索引名（`index_20261113_101010` 之类）在重建时会变，**别名**是不变的那一层。
先 `ensure_initialized()` 建好指针，再让 `build_vector_store` 按别名解析——
"物理索引名只有一个来源"，重建切换时不会出现两个进程看到不同索引。

## 索引损坏即拒绝启动

`store.load()` 失败不降级：带着坏索引对外服务，会让每一个问答都返回空结果，
而调用方无法区分「库里没有」与「索引坏了」。宁可起不来。
"""
from __future__ import annotations

from app.assembly.context import Assembly
from app.services.docstore import DocStore
from app.services.index_alias import IndexAliasManager
from app.services.parsers.docx import DocxParser
from app.services.parsers.ocr import OcrParser
from app.services.parsers.pdf import PdfParser
from app.services.parsers.plaintext import PlainTextParser
from app.services.parsers.router import ParserRouter
from app.services.parsers.spreadsheet import SpreadsheetParser
from app.services.retrieval.bm25 import BM25Retriever
from app.services.retrieval.hybrid import HybridRetriever
from app.services.retrieval.vector import VectorRetriever
from app.services.vectorstore import build_vector_store


def build_storage(asm: Assembly) -> None:
    """装好索引别名、向量库、BM25、文档存储、解析路由与混合检索。"""
    settings = asm.settings
    app = asm.app

    # 索引别名（重建不停机切换的真相源）：先建指针再装存储，
    # 保证"物理索引名"只有一个来源。
    alias_manager = IndexAliasManager(
        settings.data_dir,
        base_name=(
            settings.milvus_collection if settings.vector_store == "milvus" else "index"
        ),
    )
    alias_manager.ensure_initialized()
    asm.alias_manager = alias_manager
    app.state.index_alias = alias_manager

    # 存储与索引（不可用/损坏 → 拒绝启动）
    store = build_vector_store(settings, alias_manager)
    store.load()
    bm25 = BM25Retriever(k1=settings.bm25_k1, b=settings.bm25_b)
    bm25.rebuild(store.all_chunks())

    # 混合检索编排
    hybrid = HybridRetriever(
        retrievers={
            "keyword": bm25,
            "vector": VectorRetriever(asm.embeddings, store),
        },
        reranker=asm.reranker,
        per_route_top_n=settings.retrieval_per_route_top_n,
        fused_top_n=settings.retrieval_fused_top_n,
        rrf_k=settings.rrf_k,
        settings=settings,
    )

    # DocStore（父块/表格/媒体资源）+ 解析路由
    docstore = DocStore(settings.data_dir / "docstore.json")
    parser_router = ParserRouter(
        ocr_parser=OcrParser(asm.ocr_engine, settings.data_dir / "assets"),
        docx_parser=DocxParser(settings.data_dir / "assets"),
        pdf_parser=PdfParser(
            ocr_engine=asm.ocr_engine,
            assets_dir=settings.data_dir / "assets",
            ocr_enabled=settings.pdf_ocr_enabled,
            ocr_dpi=settings.pdf_ocr_dpi,
            ocr_max_pages=settings.pdf_ocr_max_pages,
            scan_min_chars=settings.pdf_scan_min_chars,
        ),
        # F3：新增表格与纯文本/代码解析器
        spreadsheet_parser=SpreadsheetParser(),
        plaintext_parser=PlainTextParser(),
    )

    asm.store = store
    asm.bm25 = bm25
    asm.hybrid = hybrid
    asm.docstore = docstore
    asm.parser_router = parser_router
    app.state.store = store
    app.state.bm25 = bm25
    app.state.hybrid = hybrid
    app.state.docstore = docstore
    app.state.parser_router = parser_router
