"""RAG 检索核心服务。

包含嵌入、检索、重排序、HyDE、BM25 等 RAG 核心组件。
"""

from app.services.retriever import RAGEngine, RetrievalResult, RerankerService, RRFusionEngine, TimeDecayScorer
from app.services.embedding import OpenAIEmbeddingService
from app.services.hyde_generator import HyDEGenerator, HyDEGenerationResult
from app.services.bm25_index_manager import BM25IndexManager, IndexStatus, SyncResult
from app.services.bm25_retriever import BM25Retriever, Document, RetrievalResult as BM25RetrievalResult

__all__ = [
    "RAGEngine",
    "RetrievalResult",
    "RerankerService",
    "RRFusionEngine",
    "TimeDecayScorer",
    "OpenAIEmbeddingService",
    "HyDEGenerator",
    "HyDEGenerationResult",
    "BM25IndexManager",
    "IndexStatus",
    "SyncResult",
    "BM25Retriever",
    "Document",
    "BM25RetrievalResult",
]
