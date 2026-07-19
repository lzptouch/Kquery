"""
P2-1 共享知识库路由：CRUD、成员管理、邀请、权限控制
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.auth import get_current_user
from app.core.logger import get_logger
from app.core.rbac import require_kb_role
from app.services.knowledge_base_service import KnowledgeBaseService
from app.models.knowledge_base import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KBMemberResponse,
    AddMemberRequest,
    UpdateMemberRoleRequest,
    KBInviteRequest,
    KBInviteResponse,
    AcceptInviteRequest,
    KBRole,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/knowledge-bases", tags=["共享知识库"])
kb_service = KnowledgeBaseService()


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """创建知识库"""
    return await kb_service.create_kb(user, request, db)


@router.get("", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出我的知识库"""
    return await kb_service.list_my_kbs(user, db)


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取知识库详情"""
    kb = await kb_service.get_kb_detail(user, kb_id, db)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在或无权访问")
    return kb


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(
    kb_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """删除知识库（仅 owner）"""
    success = await kb_service.delete_kb(user, kb_id, db)
    if not success:
        raise HTTPException(status_code=403, detail="无权删除该知识库")


# ========== 成员管理 ==========

@router.get("/{kb_id}/members", response_model=list[KBMemberResponse])
async def list_members(
    kb_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """列出成员（需 KB 访问权限）"""
    # 检查是否有至少 VIEWER 权限
    role_check = require_kb_role(KBRole.VIEWER)
    await role_check(kb_id=kb_id, user=user, db=db)
    return await kb_service.list_members(kb_id, db)


@router.post("/{kb_id}/members", status_code=status.HTTP_201_CREATED)
async def add_member(
    kb_id: str,
    request: AddMemberRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """添加成员（需 ADMIN+ 权限）"""
    role_check = require_kb_role(KBRole.ADMIN)
    await role_check(kb_id=kb_id, user=user, db=db)
    try:
        await kb_service.add_member(user, kb_id, request.username, request.role, db)
        return {"message": "成员添加成功"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{kb_id}/members/{target_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    kb_id: str,
    target_user_id: str,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """移除成员（需 ADMIN+ 权限）"""
    role_check = require_kb_role(KBRole.ADMIN)
    await role_check(kb_id=kb_id, user=user, db=db)
    await kb_service.remove_member(kb_id, target_user_id, db)


@router.put("/{kb_id}/members/{member_id}/role")
async def update_member_role(
    kb_id: str,
    member_id: str,
    request: UpdateMemberRoleRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改成员角色（需 ADMIN+ 权限）"""
    role_check = require_kb_role(KBRole.ADMIN)
    await role_check(kb_id=kb_id, user=user, db=db)
    try:
        await kb_service.update_member_role(kb_id, member_id, request.role, db)
        return {"message": "角色更新成功"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ========== 邀请管理 ==========

@router.post("/{kb_id}/invites", response_model=KBInviteResponse)
async def generate_invite(
    kb_id: str,
    request: KBInviteRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """生成邀请码（需 ADMIN+ 权限）"""
    role_check = require_kb_role(KBRole.ADMIN)
    await role_check(kb_id=kb_id, user=user, db=db)
    invite = await kb_service.generate_invite(
        user, kb_id, request.role, request.expires_in_hours, request.max_uses, db
    )
    return KBInviteResponse(
        id=invite.id,
        kb_id=invite.kb_id,
        code=invite.code,
        role=KBRole(invite.role),
        invite_url=f"/knowledge-bases/{kb_id}/join?code={invite.code}",
        expires_at=invite.expires_at,
        max_uses=int(invite.max_uses),
        used_count=int(invite.used_count),
        is_active=invite.is_active == "1",
    )


@router.post("/join")
async def accept_invite(
    request: AcceptInviteRequest,
    user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """通过邀请码加入知识库"""
    try:
        await kb_service.accept_invite(user, request.code, db)
        return {"message": "成功加入知识库"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
