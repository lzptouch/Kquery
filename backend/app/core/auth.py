"""
P2-1 认证工具：JWT Token 生成/验证、密码哈希、get_current_user 依赖
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.logger import get_logger
from app.core.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()

# 密码哈希上下文
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# HTTP Bearer 安全方案
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """对密码进行哈希"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(
    user_id: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """创建访问 Token"""
    if expires_delta is None:
        expires_delta = timedelta(hours=settings.security.jwt_expiration_hours)

    to_encode = {
        "sub": user_id,
        "type": "access",
        "exp": datetime.now(timezone.utc) + expires_delta,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(
        to_encode,
        settings.security.jwt_secret_key,
        algorithm=settings.security.jwt_algorithm,
    )


def create_refresh_token(user_id: str) -> str:
    """创建刷新 Token"""
    expires_delta = timedelta(days=settings.security.jwt_refresh_expiration_days)
    to_encode = {
        "sub": user_id,
        "type": "refresh",
        "exp": datetime.now(timezone.utc) + expires_delta,
        "iat": datetime.now(timezone.utc),
    }
    return jwt.encode(
        to_encode,
        settings.security.jwt_secret_key,
        algorithm=settings.security.jwt_algorithm,
    )


def decode_token(token: str) -> dict:
    """解码 Token，验证签名和过期"""
    try:
        payload = jwt.decode(
            token,
            settings.security.jwt_secret_key,
            algorithms=[settings.security.jwt_algorithm],
        )
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"无效的认证凭据: {str(e)}",
        )


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
):
    """FastAPI 依赖：从 Token 中获取当前用户。

    如果 FEATURE_USER_AUTH_ENABLED 关闭，则返回匿名用户（向后兼容）。
    """
    # 功能开关关闭时，返回匿名用户
    if not settings.features.user_auth_enabled:
        return _anonymous_user()

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要认证凭据",
        )

    payload = decode_token(credentials.credentials)
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="请使用访问 Token",
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的认证凭据",
        )

    from app.models.user import UserORM

    result = await db.execute(select(UserORM).where(UserORM.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户已被禁用",
        )

    return user


def _anonymous_user():
    """模拟匿名用户对象（auth 关闭时的降级方案）"""
    from types import SimpleNamespace
    return SimpleNamespace(
        id="anonymous",
        username="anonymous",
        email=None,
        display_name="匿名用户",
        is_active=True,
        is_superuser=False,
    )
