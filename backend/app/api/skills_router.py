"""
P2-2 Skills 路由：Skill 市场管理 API
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.auth import get_current_user
from app.core.logger import get_logger
from app.services.skill_registry import skill_registry
from app.models.skill import (
    SkillCreateRequest,
    SkillUpdateRequest,
    SkillResponse,
    SkillMatchRequest,
    SkillMatchResult,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/skills", tags=["Skills 生态"])


@router.get("", response_model=list[SkillResponse])
async def list_skills(include_disabled: bool = False):
    """列出所有 Skill"""
    return skill_registry.list_skills(include_disabled=include_disabled)


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(skill_id: str):
    """获取 Skill 详情"""
    skill = skill_registry.get_skill(skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return skill


@router.post("", response_model=SkillResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(request: SkillCreateRequest, user=Depends(get_current_user)):
    """创建自定义 Skill"""
    return skill_registry.create_skill(request)


@router.put("/{skill_id}", response_model=SkillResponse)
async def update_skill(skill_id: str, request: SkillUpdateRequest):
    """更新 Skill"""
    skill = skill_registry.update_skill(skill_id, request)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill 不存在或不可修改")
    return skill


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(skill_id: str):
    """删除自定义 Skill"""
    if not skill_registry.delete_skill(skill_id):
        raise HTTPException(status_code=404, detail="Skill 不存在或为内置 Skill，不可删除")


@router.post("/{skill_id}/enable")
async def enable_skill(skill_id: str):
    """启用 Skill"""
    if not skill_registry.enable_skill(skill_id):
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"message": "Skill 已启用"}


@router.post("/{skill_id}/disable")
async def disable_skill(skill_id: str):
    """禁用 Skill"""
    if not skill_registry.disable_skill(skill_id):
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return {"message": "Skill 已禁用"}


@router.post("/match", response_model=list[SkillMatchResult])
async def match_skills(request: SkillMatchRequest):
    """根据 query 匹配最合适的 Skill"""
    return await skill_registry.match_skills(request.query, request.top_k)
