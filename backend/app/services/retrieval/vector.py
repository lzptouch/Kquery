"""向量语义检索路：复用 的向量索引实现，包装为统一检索器接口。"""
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
        return self.search_with_vector(query_vec, top_n, tenant_id, doc_id=doc_id, filters=filters)

    def search_with_vector(
        self, query_vec: list[float], top_n: int, tenant_id: str, doc_id: str | None = None,
        filters=None,
    ) -> list[RetrievalCandidate]:
        """向量已预编码的检索（批次 3：多查询批量编码复用，避免逐条 embed_query）。"""
        # filters 透传给存储层（Milvus 下推表达式 / FAISS 迭代扩取）
        hits = self._store.search(query_vec, top_n, tenant_id, doc_id=doc_id, filters=filters)
        return [RetrievalCandidate(chunk=chunk, score=score) for chunk, score in hits]
