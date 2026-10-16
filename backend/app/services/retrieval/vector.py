"""向量语义检索路：复用阶段 1 的向量索引实现，包装为统一检索器接口。"""
from app.services.embeddings import Embeddings
from app.services.retrieval.base import RetrievalCandidate
from app.services.vectorstore import VectorStore


class VectorRetriever:
    name = "vector"

    def __init__(self, embeddings: Embeddings, store: VectorStore):
        self._embeddings = embeddings
        self._store = store

    def search(
        self, query: str, top_n: int, tenant_id: str, doc_id: str | None = None,
        filters=None,
    ) -> list[RetrievalCandidate]:
        query_vec = self._embeddings.embed_query(query)
        # TASK-142：filters 透传给存储层（Milvus 下推表达式 / FAISS 迭代扩取）
        hits = self._store.search(query_vec, top_n, tenant_id, doc_id=doc_id, filters=filters)
        return [RetrievalCandidate(chunk=chunk, score=score) for chunk, score in hits]
