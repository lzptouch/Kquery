"""
P2-2 Skills 生态数据模型：Skill Pydantic Schema
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field


class SkillBase(BaseModel):
    """Skill 基础字段"""
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field(..., min_length=1, max_length=500)
    system_prompt: str = Field(..., min_length=1)
    tools: List[str] = Field(default_factory=list)
    default_kb_id: Optional[str] = None
    example_questions: List[str] = Field(default_factory=list, max_length=10)
    model: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    is_builtin: bool = False
    is_enabled: bool = True
    author: Optional[str] = None
    version: str = "1.0.0"


class SkillCreateRequest(SkillBase):
    """创建 Skill 请求"""
    pass


class SkillUpdateRequest(BaseModel):
    """更新 Skill 请求（所有字段可选）"""
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    tools: Optional[List[str]] = None
    default_kb_id: Optional[str] = None
    example_questions: Optional[List[str]] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    is_enabled: Optional[bool] = None
    version: Optional[str] = None


class SkillResponse(SkillBase):
    """Skill 响应（含系统字段）"""
    id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    model_config = {"from_attributes": True}


class SkillMatchRequest(BaseModel):
    """Skill 匹配请求"""
    query: str = Field(..., min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)


class SkillMatchResult(BaseModel):
    """Skill 匹配结果"""
    skill_id: str
    skill_name: str
    confidence: float = Field(ge=0, le=1)
    reason: str = ""
