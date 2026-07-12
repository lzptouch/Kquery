"""
BM25 索引管理器

职责：
- 管理 BM25 索引的生命周期
- 从 Milvus 同步文档到 BM25 索引
- 提供索引查询接口
- 支持增量更新和自动缓存

主要功能：
1. 索引同步：
   - 全量同步：从 Milvus 读取所有文档构建索引
   - 增量同步：只同步新增/修改的文档
   - 自动同步：定时触发同步任务

2. 索引管理：
   - 构建索引
   - 更新索引
   - 删除索引
   - 索引持久化

3. 检索功能：
   - 执行 BM25 检索
   - 返回检索结果

4. 状态管理：
   - 获取索引状态
   - 索引健康检查

设计原则：
- 独立模块，不依赖现有向量检索系统
- 支持按客户 ID 隔离索引
- 支持索引缓存和持久化
- 支持增量更新，避免全量重建

使用示例：
    manager = BM25IndexManager()
    await manager.sync_from_milvus(customer_id="customer_123")
    results = await manager.retrieve(customer_id="customer_123", query="人工智能", top_k=10)
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_current_file = Path(__file__).resolve()
_kquery_root = _current_file.parent.parent.parent
_kquery_root_str = str(_kquery_root)
if _kquery_root_str not in sys.path:
    sys.path.insert(0, _kquery_root_str)

from app.services.bm25_retriever import (
    BM25Retriever,
    Document,
    RetrievalResult,
)
from app.core.logger import get_logger

logger = get_logger(__name__)


def validate_customer_id(customer_id: str) -> str:
    """验证客户 ID。

    Args:
        customer_id: 客户 ID

    Returns:
        验证后的客户 ID

    Raises:
        ValueError: 如果客户 ID 无效
    """
    if not customer_id:
        raise ValueError("customer_id cannot be empty")

    if not isinstance(customer_id, str):
        raise ValueError("customer_id must be a string")

    customer_id = customer_id.strip()

    if not customer_id:
        raise ValueError("customer_id cannot be empty or whitespace only")

    if len(customer_id) > 256:
        raise ValueError("customer_id cannot exceed 256 characters")

    forbidden_chars = {"..", "/", "\\", "\0", "\n", "\r", "\t"}
    for char in forbidden_chars:
        if char in customer_id:
            raise ValueError(f"customer_id cannot contain forbidden character: {repr(char)}")

    return customer_id


def validate_query(query: str) -> str:
    """验证查询文本。

    Args:
        query: 查询文本

    Returns:
        验证后的查询文本

    Raises:
        ValueError: 如果查询文本无效
    """
    if query is None:
        raise ValueError("query cannot be None")

    if not isinstance(query, str):
        raise ValueError("query must be a string")

    query = query.strip()

    if len(query) > 10000:
        raise ValueError("query cannot exceed 10000 characters")

    return query


def validate_top_k(top_k: int) -> int:
    """验证 top_k 参数。

    Args:
        top_k: 返回的最相关文档数量

    Returns:
        验证后的 top_k

    Raises:
        ValueError: 如果 top_k 无效
    """
    if not isinstance(top_k, int):
        raise ValueError("top_k must be an integer")

    if top_k < 1:
        raise ValueError("top_k must be at least 1")

    if top_k > 1000:
        raise ValueError("top_k cannot exceed 1000")

    return top_k


def validate_min_score(min_score: float) -> float:
    """验证 min_score 参数。

    Args:
        min_score: 最小相关性分数阈值

    Returns:
        验证后的 min_score

    Raises:
        ValueError: 如果 min_score 无效
    """
    if not isinstance(min_score, (int, float)):
        raise ValueError("min_score must be a number")

    min_score = float(min_score)

    if min_score < 0:
        raise ValueError("min_score cannot be negative")

    return min_score


def sanitize_customer_id(customer_id: str) -> str:
    """清理客户 ID 以防止路径遍历攻击。

    Args:
        customer_id: 客户 ID

    Returns:
        清理后的客户 ID
    """
    customer_id = customer_id.strip()
    customer_id = customer_id.replace("..", "")
    customer_id = customer_id.replace("/", "_")
    customer_id = customer_id.replace("\\", "_")
    customer_id = customer_id.replace("\0", "")
    customer_id = customer_id.replace("\n", "")
    customer_id = customer_id.replace("\r", "")
    customer_id = customer_id.replace("\t", "")
    return customer_id


@dataclass
class IndexStatus:
    """索引状态。

    Attributes:
        customer_id: 客户 ID
        is_indexed: 是否已构建索引
        document_count: 索引中的文档数量
        last_sync_time: 最后同步时间
        index_hash: 索引哈希值
        index_path: 索引文件路径
        is_healthy: 索引是否健康
    """

    customer_id: str
    is_indexed: bool
    document_count: int
    last_sync_time: Optional[datetime]
    index_hash: str
    index_path: str
    is_healthy: bool


@dataclass
class SyncResult:
    """同步结果。

    Attributes:
        customer_id: 客户 ID
        sync_type: 同步类型（full/incremental）
        documents_added: 新增文档数量
        documents_updated: 更新文档数量
        documents_removed: 移除文档数量
        sync_time_ms: 同步耗时（毫秒）
        success: 是否成功
        error_message: 错误信息
    """

    customer_id: str
    sync_type: str
    documents_added: int
    documents_updated: int
    documents_removed: int
    sync_time_ms: float
    success: bool
    error_message: Optional[str] = None


class BM25IndexManager:
    """BM25 索引管理器。

    管理 BM25 索引的生命周期，包括构建、更新、持久化和加载。
    支持增量更新和自动缓存。

    Attributes:
        cache_dir: 索引缓存目录
        auto_save: 是否自动保存索引
    """

    def __init__(
        self,
        cache_dir: str | Path | None = None,
        auto_save: bool = True,
        **bm25_kwargs,
    ):
        """初始化索引管理器。

        Args:
            cache_dir: 索引缓存目录
            auto_save: 是否自动保存索引
            **bm25_kwargs: 传递给 BM25Retriever 的参数
        """
        if cache_dir is None:
            cache_dir = Path.cwd() / ".cache" / "bm25"
        else:
            cache_dir = Path(cache_dir)

        self.cache_dir = cache_dir
        self.auto_save = auto_save
        self.bm25_kwargs = bm25_kwargs

        self.retrievers: dict[str, BM25Retriever] = {}
        self._sync_metadata: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._customer_locks: dict[str, asyncio.Lock] = {}
        self._load_sync_metadata()

    @property
    def index_dir(self) -> Path:
        """获取索引目录。"""
        return self.cache_dir

    def _get_index_path(self, customer_id: str) -> Path:
        """获取指定客户的索引文件路径。

        Args:
            customer_id: 客户 ID

        Returns:
            索引文件路径
        """
        safe_customer_id = sanitize_customer_id(customer_id)
        return self.cache_dir / f"{safe_customer_id}.pkl"

    def _get_metadata_path(self) -> Path:
        """获取元数据文件路径。

        Returns:
            元数据文件路径
        """
        return self.cache_dir / "metadata.json"

    def _load_sync_metadata(self) -> None:
        """加载同步元数据。"""
        metadata_path = self._get_metadata_path()
        if metadata_path.exists():
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    self._sync_metadata = json.load(f)
                logger.info(f"Loaded sync metadata from {metadata_path}")
            except Exception as e:
                logger.warning(f"Failed to load sync metadata: {e!r}")
                self._sync_metadata = {}
        else:
            self._sync_metadata = {}

    def _save_sync_metadata(self) -> None:
        """保存同步元数据。"""
        metadata_path = self._get_metadata_path()
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(metadata_path, "w", encoding="utf-8") as f:
                json.dump(self._sync_metadata, f, ensure_ascii=False, indent=2, default=str)
            logger.debug(f"Saved sync metadata to {metadata_path}")
        except Exception as e:
            logger.warning(f"Failed to save sync metadata: {e!r}")

    def _get_customer_metadata(self, customer_id: str) -> dict[str, Any]:
        """获取指定客户的元数据。

        Args:
            customer_id: 客户 ID

        Returns:
            客户元数据字典
        """
        return self._sync_metadata.get(customer_id, {})

    def _update_customer_metadata(self, customer_id: str, **kwargs) -> None:
        """更新指定客户的元数据。

        Args:
            customer_id: 客户 ID
            **kwargs: 要更新的元数据字段
        """
        safe_customer_id = sanitize_customer_id(customer_id)
        if safe_customer_id not in self._sync_metadata:
            self._sync_metadata[safe_customer_id] = {}
        self._sync_metadata[safe_customer_id].update(kwargs)
        self._save_sync_metadata()

    def _get_retriever(self, customer_id: str) -> BM25Retriever:
        """获取或创建指定客户的检索器。

        Args:
            customer_id: 客户 ID

        Returns:
            BM25 检索器实例
        """
        if customer_id not in self.retrievers:
            self.retrievers[customer_id] = BM25Retriever(**self.bm25_kwargs)
        return self.retrievers[customer_id]

    def _get_customer_lock(self, customer_id: str) -> asyncio.Lock:
        """获取或创建指定客户的锁。

        使用细粒度锁策略，每个客户独立锁，避免全局阻塞。

        Args:
            customer_id: 客户 ID

        Returns:
            客户级别的异步锁
        """
        if customer_id not in self._customer_locks:
            self._customer_locks[customer_id] = asyncio.Lock()
        return self._customer_locks[customer_id]

    async def sync_from_milvus(
        self,
        customer_id: str,
        collection_name: str = "messages",
        force_full_sync: bool = False,
    ) -> SyncResult:
        """从 Milvus 全量同步文档到 BM25 索引。

        使用客户级别的细粒度锁，避免全局阻塞。

        Args:
            customer_id: 客户 ID
            collection_name: Milvus 集合名称
            force_full_sync: 是否强制全量同步

        Returns:
            同步结果
        """
        customer_id = validate_customer_id(customer_id)
        customer_lock = self._get_customer_lock(customer_id)

        async with customer_lock:
            # 模拟同步过程（实际项目中需要连接到 Milvus 数据库）
            start_time = time.time()

            try:
                logger.info(
                    f"Starting sync for customer {customer_id} from collection {collection_name}"
                )

                # 模拟文档获取（实际项目中需要从 Milvus 获取真实文档）
                documents = await self._fetch_documents_from_milvus(customer_id, collection_name)

                retriever = self._get_retriever(customer_id)
                retriever.build_index(documents)

                if self.auto_save:
                    self.save_index(customer_id)

                sync_time_ms = (time.time() - start_time) * 1000

                self._update_customer_metadata(
                    customer_id,
                    last_sync_time=int(time.time()),
                    last_full_sync_time=int(time.time()),
                    document_count=len(documents),
                    index_hash=retriever.get_index_hash(),
                )

                result = SyncResult(
                    customer_id=customer_id,
                    sync_type="full",
                    documents_added=len(documents),
                    documents_updated=0,
                    documents_removed=0,
                    sync_time_ms=sync_time_ms,
                    success=True,
                )

                logger.info(f"Sync completed for customer {customer_id}: {result}")
                return result

            except Exception as e:
                sync_time_ms = (time.time() - start_time) * 1000
                logger.error(f"Sync failed for customer {customer_id}: {e}", exc_info=True)
                return SyncResult(
                    customer_id=customer_id,
                    sync_type="full",
                    documents_added=0,
                    documents_updated=0,
                    documents_removed=0,
                    sync_time_ms=sync_time_ms,
                    success=False,
                    error_message=str(e),
                )

    async def _fetch_documents_from_milvus(
        self, customer_id: str, collection_name: str
    ) -> list[Document]:
        """从 Milvus 获取文档（模拟实现）。

        Args:
            customer_id: 客户 ID
            collection_name: 集合名称

        Returns:
            文档列表
        """
        # 模拟从 Milvus 获取文档
        # 实际项目中需要实现真实的 Milvus 查询逻辑
        logger.info(
            f"Fetching documents for customer {customer_id} from collection {collection_name}"
        )

        # 返回模拟文档（实际项目中需要替换为真实数据）
        documents = [
            Document(
                doc_id=f"doc_{i}",
                content=f"文档 {i} 的内容，关于客户 {customer_id} 的信息",
                metadata={
                    "customer_id": customer_id,
                    "collection_name": collection_name,
                    "timestamp": int(time.time()),
                },
            )
            for i in range(10)  # 模拟10个文档
        ]

        logger.info(f"Fetched {len(documents)} documents for customer {customer_id}")
        return documents

    async def retrieve(
        self,
        customer_id: str,
        query: str,
        top_k: int = 10,
        min_score: float = 0.0,
        auto_sync: bool = True,
    ) -> list[RetrievalResult]:
        """执行 BM25 检索。

        使用客户级别的细粒度锁，避免全局阻塞。

        Args:
            customer_id: 客户 ID
            query: 查询文本
            top_k: 返回的最相关文档数量
            min_score: 最小相关性分数阈值
            auto_sync: 如果索引不存在，是否自动同步

        Returns:
            检索结果列表，按相关性分数降序排序
        """
        customer_id = validate_customer_id(customer_id)
        query = validate_query(query)
        top_k = validate_top_k(top_k)
        min_score = validate_min_score(min_score)

        customer_lock = self._get_customer_lock(customer_id)

        async with customer_lock:
            retriever = self._get_retriever(customer_id)

            if not retriever.is_indexed:
                if auto_sync:
                    logger.info(f"Index not found for customer {customer_id}, auto-syncing...")
                    await self.sync_from_milvus(customer_id)
                else:
                    logger.warning(
                        f"Index not found for customer {customer_id} and auto_sync is disabled"
                    )
                    return []

            return retriever.retrieve(query, top_k=top_k, min_score=min_score)

    async def batch_retrieve(
        self,
        customer_id: str,
        queries: list[str],
        top_k: int = 10,
        min_score: float = 0.0,
        auto_sync: bool = True,
    ) -> dict[str, list[RetrievalResult]]:
        """批量检索。

        使用客户级别的细粒度锁，避免全局阻塞。

        Args:
            customer_id: 客户 ID
            queries: 查询文本列表
            top_k: 每个查询返回的最相关文档数量
            min_score: 最小相关性分数阈值
            auto_sync: 如果索引不存在，是否自动同步

        Returns:
            查询到检索结果的映射字典
        """
        customer_id = validate_customer_id(customer_id)
        top_k = validate_top_k(top_k)
        min_score = validate_min_score(min_score)

        if not isinstance(queries, list):
            raise ValueError("queries must be a list")

        validated_queries = [validate_query(q) for q in queries]

        customer_lock = self._get_customer_lock(customer_id)

        async with customer_lock:
            retriever = self._get_retriever(customer_id)

            if not retriever.is_indexed:
                if auto_sync:
                    logger.info(f"Index not found for customer {customer_id}, auto-syncing...")
                    await self.sync_from_milvus(customer_id)
                else:
                    logger.warning(
                        f"Index not found for customer {customer_id} and auto_sync is disabled"
                    )
                    return {q: [] for q in validated_queries}

            return retriever.batch_retrieve(validated_queries, top_k=top_k, min_score=min_score)

    def save_index(self, customer_id: str) -> None:
        """保存索引到缓存目录。

        Args:
            customer_id: 客户 ID
        """
        retriever = self._get_retriever(customer_id)
        index_path = self._get_index_path(customer_id)

        logger.info(f"Saving BM25 index for customer {customer_id} to {index_path}")

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        retriever.save_index(index_path)

    def load_index(self, customer_id: str) -> bool:
        """从缓存目录加载索引。

        Args:
            customer_id: 客户 ID

        Returns:
            是否成功加载
        """
        index_path = self._get_index_path(customer_id)

        if not index_path.exists():
            logger.info(f"Index file not found for customer {customer_id}: {index_path}")
            return False

        logger.info(f"Loading BM25 index for customer {customer_id} from {index_path}")

        try:
            retriever = self._get_retriever(customer_id)
            retriever.load_index(index_path)
            return True
        except Exception as e:
            logger.error(f"Failed to load index for customer {customer_id}: {e!r}")
            return False

    def clear_index(self, customer_id: str) -> None:
        """清空指定客户的索引。

        Args:
            customer_id: 客户 ID
        """
        retriever = self._get_retriever(customer_id)
        retriever.clear_index()

        index_path = self._get_index_path(customer_id)
        if index_path.exists():
            index_path.unlink()
            logger.info(f"Index file deleted for customer {customer_id}: {index_path}")

        if customer_id in self._sync_metadata:
            del self._sync_metadata[customer_id]
            self._save_sync_metadata()

        logger.info(f"Index cleared for customer {customer_id}")

    def get_index_status(self, customer_id: str) -> IndexStatus:
        """获取索引状态。

        Args:
            customer_id: 客户 ID

        Returns:
            索引状态
        """
        retriever = self._get_retriever(customer_id)
        metadata = self._get_customer_metadata(customer_id)
        index_path = self._get_index_path(customer_id)

        last_sync_time = metadata.get("last_sync_time")
        if last_sync_time:
            try:
                last_sync_time = datetime.fromtimestamp(last_sync_time, tz=timezone.utc)
            except (ValueError, OSError):
                last_sync_time = None

        is_healthy = retriever.is_indexed and index_path.exists()

        return IndexStatus(
            customer_id=customer_id,
            is_indexed=retriever.is_indexed,
            document_count=retriever.document_count,
            last_sync_time=last_sync_time,
            index_hash=retriever.get_index_hash(),
            index_path=str(index_path),
            is_healthy=is_healthy,
        )

    def get_all_index_statuses(self) -> list[IndexStatus]:
        """获取所有客户的索引状态。

        Returns:
            索引状态列表
        """
        statuses = []
        for customer_id in self._sync_metadata.keys():
            statuses.append(self.get_index_status(customer_id))
        return statuses

    async def health_check(self, customer_id: str) -> bool:
        """检查索引健康状态。

        Args:
            customer_id: 客户 ID

        Returns:
            索引是否健康
        """
        status = self.get_index_status(customer_id)
        return status.is_healthy

    async def rebuild_index(
        self,
        customer_id: str,
        collection_name: str = "messages",
    ) -> SyncResult:
        """重建索引。

        Args:
            customer_id: 客户 ID
            collection_name: Milvus 集合名称

        Returns:
            同步结果
        """
        logger.info(f"Rebuilding index for customer {customer_id}")

        self.clear_index(customer_id)
        return await self.sync_from_milvus(customer_id, collection_name, force_full_sync=True)

    def close(self) -> None:
        """关闭管理器，释放资源。"""
        logger.info("Closing BM25IndexManager")
        self.retrievers.clear()
