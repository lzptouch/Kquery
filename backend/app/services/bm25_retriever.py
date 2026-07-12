"""
BM25 关键词检索模块

职责：
- 提供 BM25 关键词检索功能，与向量检索形成混合检索策略
- 管理文档索引的构建、更新和缓存
- 支持增量索引更新和查询优化

主要功能：
1. 索引管理：
   - 构建文档索引
   - 增量更新索引
   - 索引持久化与加载

2. 检索功能：
   - 关键词检索
   - 结果排序
   - 分数计算

设计原则：
- 独立模块，不依赖现有向量检索系统
- 支持中文分词（使用 jieba）
- 提供高效的索引缓存机制
- 支持增量更新，避免全量重建

使用示例：
    retriever = BM25Retriever()
    retriever.build_index(documents)
    results = retriever.retrieve(query, top_k=10)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable
from dataclasses import dataclass, field
from collections import defaultdict
import pickle
import hashlib
import time

_current_file = Path(__file__).resolve()
_kquery_root = _current_file.parent.parent.parent
_kquery_root_str = str(_kquery_root)
if _kquery_root_str not in sys.path:
    sys.path.insert(0, _kquery_root_str)

from app.core.logger import get_logger

logger = get_logger(__name__)

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False
    logger.warning("jieba library not available. Chinese text will be processed without segmentation.")

try:
    from rank_bm25 import BM25Okapi, BM25L, BM25Plus
    RANK_BM25_AVAILABLE = True
except ImportError:
    RANK_BM25_AVAILABLE = False
    logger.warning("rank_bm25 library not available. BM25 retrieval will not work.")


@dataclass
class Document:
    """文档数据结构。

    Attributes:
        doc_id: 文档唯一标识符
        content: 文档内容
        metadata: 文档元数据（如来源、时间戳等）
    """
    doc_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.doc_id:
            raise ValueError("doc_id cannot be empty")
        if not self.content:
            raise ValueError("content cannot be empty")


@dataclass
class RetrievalResult:
    """检索结果。

    Attributes:
        doc_id: 文档唯一标识符
        score: BM25 相关性分数
        rank: 排名（从 0 开始）
        metadata: 文档元数据
    """
    doc_id: str
    score: float
    rank: int
    metadata: dict[str, Any] = field(default_factory=dict)


class TextTokenizer:
    """文本分词器。

    支持中文和英文分词，使用 jieba 进行中文分词。
    """

    @staticmethod
    def tokenize(text: str) -> list[str]:
        """对文本进行分词。

        Args:
            text: 待分词的文本

        Returns:
            分词结果列表
        """
        if not text:
            return []

        text = text.strip()
        if not text:
            return []

        if JIEBA_AVAILABLE:
            tokens = list(jieba.cut(text))
            return [token for token in tokens if token.strip()]
        else:
            return text.split()

    @staticmethod
    def tokenize_batch(texts: Iterable[str]) -> list[list[str]]:
        """批量分词。

        Args:
            texts: 文本列表

        Returns:
            分词结果列表的列表
        """
        return [TextTokenizer.tokenize(text) for text in texts]


class BM25Retriever:
    """BM25 检索器。

    基于 BM25 算法的关键词检索实现，支持增量索引更新和高效检索。

    Attributes:
        k1: BM25 参数，控制词频饱和度（默认 1.5）
        b: BM25 参数，控制文档长度归一化（默认 0.75）
        epsilon: BM25 参数，用于平滑 IDF（默认 0.25）
        algorithm: BM25 算法变体（okapi, l, plus）
    """

    def __init__(
        self,
        k1: float = 1.5,
        b: float = 0.75,
        epsilon: float = 0.25,
        algorithm: str = "okapi",
    ):
        """初始化 BM25 检索器。

        Args:
            k1: BM25 k1 参数
            b: BM25 b 参数
            epsilon: BM25 epsilon 参数（仅用于 okapi 算法）
            algorithm: BM25 算法变体（okapi, l, plus）
        """
        if not RANK_BM25_AVAILABLE:
            raise ImportError("rank_bm25 library is required. Install it with: pip install rank_bm25")

        self.k1 = k1
        self.b = b
        self.epsilon = epsilon
        self.algorithm = algorithm

        self._documents: dict[str, Document] = {}
        self._tokenized_docs: list[list[str]] = []
        self._doc_id_to_index: dict[str, int] = {}
        self._index_to_doc_id: dict[int, str] = {}
        self._bm25_model: Any = None
        self._index_hash: str = ""

    @property
    def document_count(self) -> int:
        """获取索引中的文档数量。"""
        return len(self._documents)

    @property
    def is_indexed(self) -> bool:
        """检查是否已构建索引。"""
        return self._bm25_model is not None

    def build_index(self, documents: Iterable[Document]) -> None:
        """构建 BM25 索引。

        Args:
            documents: 文档列表
        """
        logger.info(f"Building BM25 index with {len(list(documents))} documents...")

        start_time = time.time()

        self._documents.clear()
        self._tokenized_docs.clear()
        self._doc_id_to_index.clear()
        self._index_to_doc_id.clear()

        doc_list = list(documents)
        for doc in doc_list:
            if doc.doc_id in self._documents:
                logger.warning(f"Duplicate doc_id found: {doc.doc_id}, skipping...")
                continue

            self._documents[doc.doc_id] = doc
            tokens = TextTokenizer.tokenize(doc.content)
            self._tokenized_docs.append(tokens)
            self._doc_id_to_index[doc.doc_id] = len(self._tokenized_docs) - 1
            self._index_to_doc_id[len(self._tokenized_docs) - 1] = doc.doc_id

        if not self._tokenized_docs:
            logger.warning("No valid documents to index")
            return

        self._create_bm25_model()

        self._index_hash = self._compute_index_hash()

        elapsed = time.time() - start_time
        logger.info(f"BM25 index built successfully in {elapsed:.2f}s, {self.document_count} documents indexed")

    def add_documents(self, documents: Iterable[Document]) -> None:
        """增量添加文档到索引。

        Args:
            documents: 要添加的文档列表
        """
        logger.info(f"Adding {len(list(documents))} documents to BM25 index...")

        start_time = time.time()

        doc_list = list(documents)
        added_count = 0

        for doc in doc_list:
            if doc.doc_id in self._documents:
                logger.warning(f"Document {doc.doc_id} already exists, skipping...")
                continue

            self._documents[doc.doc_id] = doc
            tokens = TextTokenizer.tokenize(doc.content)
            self._tokenized_docs.append(tokens)
            self._doc_id_to_index[doc.doc_id] = len(self._tokenized_docs) - 1
            self._index_to_doc_id[len(self._tokenized_docs) - 1] = doc.doc_id
            added_count += 1

        if added_count > 0:
            self._create_bm25_model()
            self._index_hash = self._compute_index_hash()

        elapsed = time.time() - start_time
        logger.info(f"Added {added_count} documents to BM25 index in {elapsed:.2f}s")

    def remove_documents(self, doc_ids: Iterable[str]) -> None:
        """从索引中移除文档。

        注意：此操作需要重建索引，性能开销较大。

        Args:
            doc_ids: 要移除的文档 ID 列表
        """
        logger.info(f"Removing {len(list(doc_ids))} documents from BM25 index...")

        start_time = time.time()

        doc_ids_to_remove = set(doc_ids)
        removed_count = 0

        for doc_id in doc_ids_to_remove:
            if doc_id in self._documents:
                del self._documents[doc_id]
                removed_count += 1

        if removed_count > 0:
            self._rebuild_index()

        elapsed = time.time() - start_time
        logger.info(f"Removed {removed_count} documents from BM25 index in {elapsed:.2f}s")

    def retrieve(
        self,
        query: str,
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> list[RetrievalResult]:
        """检索相关文档。

        Args:
            query: 查询文本
            top_k: 返回的最相关文档数量
            min_score: 最小相关性分数阈值

        Returns:
            检索结果列表，按相关性分数降序排序
        """
        if not self.is_indexed:
            logger.warning("BM25 index not built, returning empty results")
            return []

        if not query:
            return []

        query_tokens = TextTokenizer.tokenize(query)

        if not query_tokens:
            return []

        scores = self._bm25_model.get_scores(query_tokens)

        results: list[tuple[int, float]] = []
        for idx, score in enumerate(scores):
            if score >= min_score:
                results.append((idx, score))

        results.sort(key=lambda x: x[1], reverse=True)

        retrieval_results = []
        for rank, (idx, score) in enumerate(results[:top_k]):
            doc_id = self._get_doc_id_by_index(idx)
            if doc_id:
                doc = self._documents[doc_id]
                result = RetrievalResult(
                    doc_id=doc_id,
                    score=float(score),
                    rank=rank,
                    metadata=doc.metadata.copy(),
                )
                retrieval_results.append(result)

        return retrieval_results

    def batch_retrieve(
        self,
        queries: Iterable[str],
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> dict[str, list[RetrievalResult]]:
        """批量检索。

        Args:
            queries: 查询文本列表
            top_k: 每个查询返回的最相关文档数量
            min_score: 最小相关性分数阈值

        Returns:
            查询到检索结果的映射字典
        """
        results = {}
        for query in queries:
            results[query] = self.retrieve(query, top_k=top_k, min_score=min_score)
        return results

    def save_index(self, file_path: str | Path) -> None:
        """保存索引到文件。

        Args:
            file_path: 保存路径
        """
        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info(f"Saving BM25 index to {file_path}...")

        start_time = time.time()

        index_data = {
            "documents": self._documents,
            "tokenized_docs": self._tokenized_docs,
            "doc_id_to_index": self._doc_id_to_index,
            "index_to_doc_id": self._index_to_doc_id,
            "k1": self.k1,
            "b": self.b,
            "epsilon": self.epsilon,
            "algorithm": self.algorithm,
            "index_hash": self._index_hash,
        }

        with open(file_path, "wb") as f:
            pickle.dump(index_data, f)

        elapsed = time.time() - start_time
        logger.info(f"BM25 index saved to {file_path} in {elapsed:.2f}s")

    def load_index(self, file_path: str | Path) -> None:
        """从文件加载索引。

        Args:
            file_path: 索引文件路径
        """
        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"Index file not found: {file_path}")

        logger.info(f"Loading BM25 index from {file_path}...")

        start_time = time.time()

        with open(file_path, "rb") as f:
            index_data = pickle.load(f)

        self._documents = index_data["documents"]
        self._tokenized_docs = index_data["tokenized_docs"]
        self._doc_id_to_index = index_data["doc_id_to_index"]
        self._index_to_doc_id = index_data.get("index_to_doc_id", {})
        self.k1 = index_data["k1"]
        self.b = index_data["b"]
        self.epsilon = index_data["epsilon"]
        self.algorithm = index_data["algorithm"]
        self._index_hash = index_data["index_hash"]

        if not self._index_to_doc_id:
            self._index_to_doc_id = {idx: doc_id for doc_id, idx in self._doc_id_to_index.items()}

        self._create_bm25_model()

        elapsed = time.time() - start_time
        logger.info(f"BM25 index loaded from {file_path} in {elapsed:.2f}s, {self.document_count} documents")

    def get_document(self, doc_id: str) -> Document | None:
        """获取文档。

        Args:
            doc_id: 文档 ID

        Returns:
            文档对象，如果不存在则返回 None
        """
        return self._documents.get(doc_id)

    def get_index_hash(self) -> str:
        """获取索引哈希值，用于检测索引变化。"""
        return self._index_hash

    def clear_index(self) -> None:
        """清空索引。"""
        self._documents.clear()
        self._tokenized_docs.clear()
        self._doc_id_to_index.clear()
        self._index_to_doc_id.clear()
        self._bm25_model = None
        self._index_hash = ""
        logger.info("BM25 index cleared")

    def _create_bm25_model(self) -> None:
        """创建 BM25 模型。"""
        if self.algorithm == "l":
            self._bm25_model = BM25L(
                self._tokenized_docs,
                k1=self.k1,
                b=self.b,
            )
        elif self.algorithm == "plus":
            self._bm25_model = BM25Plus(
                self._tokenized_docs,
                k1=self.k1,
                b=self.b,
            )
        else:
            self._bm25_model = BM25Okapi(
                self._tokenized_docs,
                k1=self.k1,
                b=self.b,
                epsilon=self.epsilon,
            )

    def _rebuild_index(self) -> None:
        """重建索引。"""
        if not self._documents:
            self._bm25_model = None
            self._index_hash = ""
            return

        self._tokenized_docs.clear()
        self._doc_id_to_index.clear()
        self._index_to_doc_id.clear()

        for doc_id, doc in self._documents.items():
            tokens = TextTokenizer.tokenize(doc.content)
            self._tokenized_docs.append(tokens)
            self._doc_id_to_index[doc_id] = len(self._tokenized_docs) - 1
            self._index_to_doc_id[len(self._tokenized_docs) - 1] = doc_id

        self._create_bm25_model()
        self._index_hash = self._compute_index_hash()

    def _compute_index_hash(self) -> str:
        """计算索引哈希值。"""
        doc_ids = sorted(self._documents.keys())
        hash_input = "|".join(doc_ids)
        return hashlib.md5(hash_input.encode()).hexdigest()

    def _get_doc_id_by_index(self, index: int) -> str | None:
        """根据索引获取文档 ID。"""
        return self._index_to_doc_id.get(index)