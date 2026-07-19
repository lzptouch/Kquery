"""
P2-1 共享知识库模型：KnowledgeBase ORM、KBMember ORM、Pydantic Schema
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, List, Literal

from pydantic import BaseModel, Field
from sqlalchemy import Column, String, DateTime, ForeignKey, Enum as SAEnum

from app.core.database import Base


# ========== 枚举 ==========

class KBRole(str, Enum):
    OWNER = "owner"
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


# ========== SQLAlchemy ORM 模型 ==========

class KnowledgeBaseORM(Base):
    """知识库 ORM 模型"""
    __tablename__ = "knowledge_bases"

    id = Column(String, primary_key=True, default=lambda: f"kb_{uuid.uuid4().hex[:12]}")
    name = Column(String(255), nullable=False)
    collection_name = Column(String(512), unique=True, nullable=False, index=True)
    owner_id = Column(String, ForeignKey("users.id"), nullable=False)
    visibility = Column(String(16), default="private")  # public / private
    description = Column(String(1024), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<KnowledgeBaseORM id={self.id} name={self.name}>"


class KBMemberORM(Base):
    """知识库成员 ORM 模型"""
    __tablename__ = "kb_members"

    id = Column(String, primary_key=True, default=lambda: f"mbr_{uuid.uuid4().hex[:12]}")
    kb_id = Column(String, ForeignKey("knowledge_bases.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(16), default=KBRole.VIEWER.value)
    joined_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<KBMemberORM kb_id={self.kb_id} user_id={self.user_id} role={self.role}>"


class KBInviteORM(Base):
    """知识库邀请码 ORM 模型"""
    __tablename__ = "kb_invites"

    id = Column(String, primary_key=True, default=lambda: f"inv_{uuid.uuid4().hex[:12]}")
    kb_id = Column(String, ForeignKey("knowledge_bases.id"), nullable=False, index=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    role = Column(String(16), default=KBRole.VIEWER.value)
    created_by = Column(String, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime, nullable=True)
    max_uses = Column(String, default="0")  # 0 = 无限
    used_count = Column(String, default="0")
    is_active = Column(String, default="1")
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<KBInviteORM kb_id={self.kb_id} code={self.code}>"


# ========== Pydantic 请求/响应模型 ==========

class KnowledgeBaseCreateRequest(BaseModel):
    """创建知识库请求"""
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1024)
    visibility: Literal["public", "private"] = "private"


class KnowledgeBaseResponse(BaseModel):
    """知识库响应"""
    id: str
    name: str
    collection_name: str
    owner_id: str
    visibility: str
    description: Optional[str] = None
    member_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class KBMemberResponse(BaseModel):
    """知识库成员响应"""
    id: str
    kb_id: str
    user_id: str
    username: str = ""
    role: KBRole
    joined_at: datetime

    model_config = {"from_attributes": True}


class AddMemberRequest(BaseModel):
    """添加成员请求"""
    username: str = Field(..., min_length=1)
    role: KBRole = KBRole.VIEWER


class UpdateMemberRoleRequest(BaseModel):
    """更新成员角色请求"""
    role: KBRole


class KBInviteRequest(BaseModel):
    """创建邀请码请求"""
    role: KBRole = KBRole.VIEWER
    expires_in_hours: Optional[int] = Field(default=72, ge=1, le=720)
    max_uses: int = Field(default=10, ge=1)


class KBInviteResponse(BaseModel):
    """邀请码响应"""
    id: str
    kb_id: str
    code: str
    role: KBRole
    invite_url: str
    expires_at: Optional[datetime] = None
    max_uses: int
    used_count: int
    is_active: bool

    model_config = {"from_attributes": True}


class AcceptInviteRequest(BaseModel):
    """接受邀请请求"""
    code: str = Field(..., min_length=1)
