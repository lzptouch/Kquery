"""知识库级别资源隔离（已废弃）。

去登录化改造后，所有知识库对所有用户共享访问权限。
本模块保留空函数/集合，确保仍引用它的代码可正常导入，但不再执行任何权限控制。
"""

from __future__ import annotations

from typing import Any, List, Optional, Set
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db


async def get_user_kb_permissions(
    db: AsyncSession,
    user_id: Optional[int] = None,
) -> Set[str]:
    """返回空集合（所有知识库均允许访问）。"""
    return set()


async def grant_kb_access(
    db: AsyncSession,
    user_id: int,
    collection_name: str,
    granted_by: Optional[int] = None,
) -> bool:
    """不再执行任何操作。"""
    return True


async def revoke_kb_access(
    db: AsyncSession,
    user_id: int,
    collection_name: str,
) -> bool:
    """不再执行任何操作。"""
    return True


async def filter_kb_by_user(
    collections: List[str],
    user: Optional[Any] = None,
    db: Optional[AsyncSession] = None,
) -> List[str]:
    """直接返回原始集合列表。"""
    return collections


async def get_current_user_kb_permissions(
    user: Optional[Any] = None,
    db: AsyncSession = Depends(get_db),
) -> Set[str]:
    """返回空集合。"""
    return set()


def require_kb_access(collection_name_param: str = "collection_name"):
    """返回一个始终允许访问的依赖函数。"""
    async def _allow_access(*args, **kwargs) -> bool:
        return True
    return _allow_access


async def check_kb_access(
    user: Optional[Any],
    collection_name: str,
    db: Optional[AsyncSession] = None,
) -> bool:
    """始终返回 True。"""
    return True


async def init_kb_isolation_tables() -> None:
    """不再执行任何操作。"""
    pass
