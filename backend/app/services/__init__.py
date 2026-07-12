"""KQuery Pro 业务逻辑层。

按领域组织：
- rag/: RAG 检索核心（嵌入、检索、重排序、HyDE、BM25）
- document/: 文档处理（文件加载、切分、图像/表格理解）
- graph/: 知识图谱构建与查询
- graphrag/: GraphRAG 增强检索
- agent/: Agent 服务
- workflow/: 工作流引擎
- integration/: 外部集成
"""

from app.services.rag_service import RAGService
from app.services.retriever import RAGEngine, RetrievalResult
from app.services.embedding import OpenAIEmbeddingService
from app.services.chunking import chunk_text, TextChunk
from app.services.hyde_generator import HyDEGenerator
from app.services.image_understanding import ImageUnderstandingService
from app.services.table_understanding import TableUnderstandingService
from app.services.lineage_service import LineageService

__all__ = [
    "RAGService",
    "RAGEngine",
    "RetrievalResult",
    "OpenAIEmbeddingService",
    "chunk_text",
    "TextChunk",
    "HyDEGenerator",
    "ImageUnderstandingService",
    "TableUnderstandingService",
    "LineageService",
]
