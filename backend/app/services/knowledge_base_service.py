"""
P2-1 共享知识库服务：创建/删除/成员管理/邀请/权限解析
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logger import get_logger
from app.core.rbac import get_user_kb_role
from app.models.knowledge_base import (
    KnowledgeBaseORM,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KBMemberORM,
    KBMemberResponse,
    KBInviteORM,
    KBRole,
)

logger = get_logger(__name__)


class KnowledgeBaseService:
    """共享知识库业务逻辑服务"""

    @staticmethod
    def _generate_collection_name(owner_id: str, name: str) -> str:
        """生成物理 Milvus 集合名：kb_{user_id}_{slug}"""
        slug = name.replace(" ", "_").lower()
        return f"kb_{owner_id}_{slug}"

    @staticmethod
    async def create_kb(
        user,
        request: KnowledgeBaseCreateRequest,
        db: AsyncSession,
    ) -> KnowledgeBaseResponse:
        """创建知识库"""
        collection_name = KnowledgeBaseService._generate_collection_name(
            user.id, request.name
        )

        # 创建业务记录
        kb = KnowledgeBaseORM(
            name=request.name,
            collection_name=collection_name,
            owner_id=user.id,
            visibility=request.visibility,
            description=request.description,
        )
        db.add(kb)
        await db.flush()

        # 自动添加 owner 为成员
        member = KBMemberORM(
            kb_id=kb.id,
            user_id=user.id,
            role=KBRole.OWNER.value,
        )
        db.add(member)
        await db.commit()
        await db.refresh(kb)

        logger.info(f"知识库创建: {kb.name} (id={kb.id}, collection={collection_name})")
        return KnowledgeBaseResponse(
            id=kb.id,
            name=kb.name,
            collection_name=kb.collection_name,
            owner_id=kb.owner_id,
            visibility=kb.visibility,
            description=kb.description,
            member_count=1,
            created_at=kb.created_at,
        )

    @staticmethod
    async def list_my_kbs(user, db: AsyncSession) -> List[KnowledgeBaseResponse]:
        """列出我创建或加入的知识库"""
        # 查询成员记录
        result = await db.execute(
            select(KBMemberORM).where(KBMemberORM.user_id == user.id)
        )
        memberships = result.scalars().all()
        kb_ids = [m.kb_id for m in memberships]

        if not kb_ids:
            return []

        kbs_result = await db.execute(
            select(KnowledgeBaseORM).where(KnowledgeBaseORM.id.in_(kb_ids))
            .order_by(KnowledgeBaseORM.created_at.desc())
        )
        kbs = kbs_result.scalars().all()

        # 统计每个 KB 的成员数
        responses = []
        for kb in kbs:
            count_result = await db.execute(
                select(KBMemberORM).where(KBMemberORM.kb_id == kb.id)
            )
            member_count = len(count_result.scalars().all())
            responses.append(KnowledgeBaseResponse(
                id=kb.id,
                name=kb.name,
                collection_name=kb.collection_name,
                owner_id=kb.owner_id,
                visibility=kb.visibility,
                description=kb.description,
                member_count=member_count,
                created_at=kb.created_at,
            ))
        return responses

    @staticmethod
    async def get_kb_detail(user, kb_id: str, db: AsyncSession) -> Optional[KnowledgeBaseResponse]:
        """获取知识库详情（需验证权限）"""
        result = await db.execute(
            select(KnowledgeBaseORM).where(KnowledgeBaseORM.id == kb_id)
        )
        kb = result.scalar_one_or_none()
        if kb is None:
            return None

        # 验证权限
        role = await get_user_kb_role(user, kb_id, db)
        if role is None and kb.visibility != "public":
            return None

        # 统计成员数
        count_result = await db.execute(
            select(KBMemberORM).where(KBMemberORM.kb_id == kb_id)
        )
        member_count = len(count_result.scalars().all())

        return KnowledgeBaseResponse(
            id=kb.id,
            name=kb.name,
            collection_name=kb.collection_name,
            owner_id=kb.owner_id,
            visibility=kb.visibility,
            description=kb.description,
            member_count=member_count,
            created_at=kb.created_at,
        )

    @staticmethod
    async def delete_kb(user, kb_id: str, db: AsyncSession) -> bool:
        """删除知识库（仅 owner 可操作）"""
        result = await db.execute(
            select(KnowledgeBaseORM).where(KnowledgeBaseORM.id == kb_id)
        )
        kb = result.scalar_one_or_none()
        if kb is None:
            return False

        if kb.owner_id != user.id and not getattr(user, "is_superuser", False):
            return False

        # 删除成员
        await db.execute(
            delete(KBMemberORM).where(KBMemberORM.kb_id == kb_id)
        )
        # 删除邀请码
        await db.execute(
            delete(KBInviteORM).where(KBInviteORM.kb_id == kb_id)
        )
        # 删除 KB
        await db.delete(kb)
        await db.commit()

        logger.info(f"知识库删除: {kb.name} (id={kb_id})")
        return True

    @staticmethod
    async def list_members(kb_id: str, db: AsyncSession) -> List[KBMemberResponse]:
        """列出知识库成员"""
        from app.models.user import UserORM

        result = await db.execute(
            select(KBMemberORM).where(KBMemberORM.kb_id == kb_id)
        )
        members = result.scalars().all()

        responses = []
        for m in members:
            user_result = await db.execute(
                select(UserORM).where(UserORM.id == m.user_id)
            )
            user = user_result.scalar_one_or_none()
            responses.append(KBMemberResponse(
                id=m.id,
                kb_id=m.kb_id,
                user_id=m.user_id,
                username=user.username if user else "unknown",
                role=KBRole(m.role),
                joined_at=m.joined_at,
            ))
        return responses

    @staticmethod
    async def add_member(user, kb_id: str, target_username: str, role: KBRole, db: AsyncSession):
        """添加成员"""
        from app.models.user import UserORM

        user_result = await db.execute(
            select(UserORM).where(UserORM.username == target_username)
        )
        target_user = user_result.scalar_one_or_none()
        if target_user is None:
            raise ValueError(f"用户 '{target_username}' 不存在")

        # 检查是否已是成员
        existing = await db.execute(
            select(KBMemberORM).where(
                KBMemberORM.kb_id == kb_id,
                KBMemberORM.user_id == target_user.id,
            )
        )
        if existing.scalar_one_or_none():
            raise ValueError("该用户已是知识库成员")

        member = KBMemberORM(
            kb_id=kb_id,
            user_id=target_user.id,
            role=role.value,
        )
        db.add(member)
        await db.commit()

        logger.info(f"成员添加: {target_username} → kb_id={kb_id}, role={role.value}")

    @staticmethod
    async def remove_member(kb_id: str, target_user_id: str, db: AsyncSession):
        """移除成员"""
        await db.execute(
            delete(KBMemberORM).where(
                KBMemberORM.kb_id == kb_id,
                KBMemberORM.user_id == target_user_id,
                KBMemberORM.role != KBRole.OWNER.value,  # 不能移除 owner
            )
        )
        await db.commit()
        logger.info(f"成员移除: user_id={target_user_id} from kb_id={kb_id}")

    @staticmethod
    async def update_member_role(kb_id: str, member_id: str, new_role: KBRole, db: AsyncSession):
        """更新成员角色"""
        result = await db.execute(
            select(KBMemberORM).where(KBMemberORM.id == member_id)
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise ValueError("成员不存在")

        if KBRole(member.role) == KBRole.OWNER:
            raise ValueError("不能修改 owner 的角色")

        member.role = new_role.value
        await db.commit()
        logger.info(f"角色更新: member_id={member_id} → {new_role.value}")

    @staticmethod
    async def generate_invite(
        user, kb_id: str, role: KBRole, expires_in_hours: int, max_uses: int, db: AsyncSession
    ) -> KBInviteORM:
        """生成邀请码"""
        code = f"KQ-{uuid.uuid4().hex[:8].upper()}"
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)

        invite = KBInviteORM(
            kb_id=kb_id,
            code=code,
            role=role.value,
            created_by=user.id,
            expires_at=expires_at,
            max_uses=str(max_uses),
        )
        db.add(invite)
        await db.commit()
        await db.refresh(invite)

        return invite

    @staticmethod
    async def accept_invite(user, code: str, db: AsyncSession):
        """接受邀请"""
        result = await db.execute(
            select(KBInviteORM).where(KBInviteORM.code == code)
        )
        invite = result.scalar_one_or_none()
        if invite is None:
            raise ValueError("邀请码无效")

        if invite.is_active != "1":
            raise ValueError("邀请码已失效")

        if invite.expires_at and invite.expires_at < datetime.now(timezone.utc):
            raise ValueError("邀请码已过期")

        max_uses = int(invite.max_uses)
        used = int(invite.used_count)
        if max_uses > 0 and used >= max_uses:
            raise ValueError("邀请码已用完")

        # 添加成员
        await KnowledgeBaseService.add_member(
            user, invite.kb_id, user.username, KBRole(invite.role), db
        )

        # 更新使用计数
        invite.used_count = str(used + 1)
        if max_uses > 0 and int(invite.used_count) >= max_uses:
            invite.is_active = "0"
        await db.commit()

        logger.info(f"邀请码接受: user={user.username}, code={code}")

    @staticmethod
    async def resolve_collection_name(user, kb_id: str, db: AsyncSession) -> str:
        """将业务 kb_id 映射为物理 Milvus 集合名并校验权限"""
        result = await db.execute(
            select(KnowledgeBaseORM).where(KnowledgeBaseORM.id == kb_id)
        )
        kb = result.scalar_one_or_none()
        if kb is None:
            raise ValueError(f"知识库 {kb_id} 不存在")

        role = await get_user_kb_role(user, kb_id, db)
        if role is None and kb.visibility != "public":
            raise PermissionError("无权访问该知识库")

        return kb.collection_name
