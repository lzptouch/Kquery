"""
P2-1 RBAC 权限控制：角色枚举、权限断言、FastAPI 依赖
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logger import get_logger
from app.models.knowledge_base import KBRole, KBMemberORM, KnowledgeBaseORM

logger = get_logger(__name__)


class Permission:
    """操作权限常量"""
    VIEW = "view"
    UPLOAD = "upload"
    EDIT = "edit"
    DELETE = "delete"
    MANAGE_MEMBERS = "manage_members"


# 角色 → 权限映射
ROLE_PERMISSIONS = {
    KBRole.OWNER: {
        Permission.VIEW, Permission.UPLOAD, Permission.EDIT,
        Permission.DELETE, Permission.MANAGE_MEMBERS,
    },
    KBRole.ADMIN: {
        Permission.VIEW, Permission.UPLOAD, Permission.EDIT,
        Permission.MANAGE_MEMBERS,
    },
    KBRole.EDITOR: {
        Permission.VIEW, Permission.UPLOAD, Permission.EDIT,
    },
    KBRole.VIEWER: {
        Permission.VIEW,
    },
}


def has_permission(user_role: KBRole, action: str) -> bool:
    """检查指定角色是否有某个操作权限"""
    perms = ROLE_PERMISSIONS.get(user_role, set())
    return action in perms


async def get_user_kb_role(
    user, kb_id: str, db: AsyncSession
) -> Optional[KBRole]:
    """获取用户在指定知识库中的角色"""
    # 超级管理员免检
    if getattr(user, "is_superuser", False):
        return KBRole.OWNER

    result = await db.execute(
        select(KBMemberORM).where(
            KBMemberORM.kb_id == kb_id,
            KBMemberORM.user_id == user.id,
        )
    )
    member = result.scalar_one_or_none()
    if member is None:
        return None

    return KBRole(member.role)


async def check_kb_public(kb_id: str, db: AsyncSession) -> bool:
    """检查知识库是否公开"""
    result = await db.execute(
        select(KnowledgeBaseORM).where(KnowledgeBaseORM.id == kb_id)
    )
    kb = result.scalar_one_or_none()
    if kb is None:
        return False
    return kb.visibility == "public"


def require_kb_role(min_role: KBRole):
    """FastAPI 依赖工厂：要求用户在指定知识库中至少拥有某角色。

    用法：
        @router.post("/{kb_id}/upload")
        async def upload(kb_id: str, user=Depends(require_kb_role(KBRole.EDITOR))):
            ...
    """

    async def dependency(
        kb_id: str = None,
        user=None,
        db: AsyncSession = Depends(get_db),
    ):
        from app.core.auth import get_current_user

        if user is None:
            return None  # 由调用方自行处理

        role = await get_user_kb_role(user, kb_id, db)
        if role is None:
            # 检查是否公开知识库 + 只需 VIEW 权限
            if min_role == KBRole.VIEWER:
                if await check_kb_public(kb_id, db):
                    return user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="您没有访问此知识库的权限",
            )

        # 权限层级：owner > admin > editor > viewer
        role_rank = {
            KBRole.OWNER: 4,
            KBRole.ADMIN: 3,
            KBRole.EDITOR: 2,
            KBRole.VIEWER: 1,
        }
        if role_rank.get(role, 0) < role_rank.get(min_role, 0):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要 {min_role.value} 及以上权限，当前角色为 {role.value}",
            )

        return user

    return dependency


def require_permission(permission: str):
    """FastAPI 依赖工厂：要求用户在指定知识库中拥有特定操作权限。

    用法：
        @router.post("/{kb_id}/upload")
        async def upload(kb_id: str, user=Depends(require_permission(Permission.UPLOAD))):
            ...
    """

    async def dependency(
        kb_id: str = None,
        user=None,
        db: AsyncSession = Depends(get_db),
    ):
        if user is None:
            return None

        role = await get_user_kb_role(user, kb_id, db)
        if role is None:
            if permission == Permission.VIEW and await check_kb_public(kb_id, db):
                return user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="您没有访问此知识库的权限",
            )

        if not has_permission(role, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"当前角色 {role.value} 没有 '{permission}' 权限",
            )

        return user

    return dependency
