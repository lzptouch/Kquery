"""
Kquery异常定义（兼容层）
委托给 app.core.exceptions 实现。
"""

from app.core.exceptions import (  # noqa: F401
    KQueryBaseException as KqueryError,
    ValidationError,
    ResourceNotFoundError,
    ExternalServiceError,
    ErrorCode,
)

__all__ = [
    "KqueryError",
    "ValidationError",
    "ResourceNotFoundError",
    "ExternalServiceError",
    "ErrorCode",
]
