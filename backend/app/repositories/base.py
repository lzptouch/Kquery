"""Repository 抽象基类。

所有 Repository 实现应继承此类，确保统一接口。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, TypeVar

T = TypeVar("T")


class BaseRepository(ABC):
    """Repository 抽象基类。"""

    @abstractmethod
    async def create(self, item: T) -> T:
        ...

    @abstractmethod
    async def get(self, id: str) -> Optional[T]:
        ...

    @abstractmethod
    async def delete(self, id: str) -> bool:
        ...


class MilvusRepository(ABC):
    """Milvus 向量数据库 Repository 基类。"""

    @abstractmethod
    async def insert(self, collection_name: str, data: Dict[str, Any]) -> bool:
        ...

    @abstractmethod
    async def search(
        self,
        collection_name: str,
        vector: List[float],
        top_k: int = 10,
        output_fields: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        ...

    @abstractmethod
    async def delete_by_id(self, collection_name: str, id: str) -> bool:
        ...

    @abstractmethod
    async def get_stats(self, collection_name: str) -> Dict[str, Any]:
        ...
