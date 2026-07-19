"""
P2-1 用户模型：UserORM SQLAlchemy 模型 & Pydantic Schema
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field
from sqlalchemy import Column, String, DateTime, Boolean

from app.core.database import Base


class UserORM(Base):
    """用户 ORM 模型"""
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=lambda: f"usr_{uuid.uuid4().hex[:12]}")
    username = Column(String(64), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    display_name = Column(String(128), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self) -> str:
        return f"<UserORM id={self.id} username={self.username}>"


# ========== Pydantic 请求/响应模型 ==========

class UserRegisterRequest(BaseModel):
    """用户注册请求"""
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=6, max_length=128)
    email: Optional[str] = Field(None, max_length=255)
    display_name: Optional[str] = Field(None, max_length=128)


class UserLoginRequest(BaseModel):
    """用户登录请求"""
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenResponse(BaseModel):
    """Token 响应"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: dict


class UserProfileResponse(BaseModel):
    """用户资料响应"""
    id: str
    username: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    is_active: bool = True
    is_superuser: bool = False
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class RefreshTokenRequest(BaseModel):
    """刷新 Token 请求"""
    refresh_token: str = Field(..., min_length=1)
