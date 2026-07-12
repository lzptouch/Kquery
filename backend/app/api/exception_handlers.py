"""KQuery Pro 全局异常处理器。

将自定义异常映射为统一的HTTP响应格式。
在FastAPI应用启动时注册所有异常处理器。

使用示例：
    from fastapi import FastAPI
    from app.api.exception_handlers import register_exception_handlers
    
    app = FastAPI()
    register_exception_handlers(app)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException as StarletteHTTPError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException

from app.core.exceptions import (
    ExternalServiceError,
    KQueryBaseException,
    RateLimitError,
    ResourceNotFoundError,
    ValidationError,
)
from app.core.logger import get_logger

logger = get_logger(__name__)


async def kquery_base_exception_handler(
    request: Request,
    exc: KQueryBaseException,
) -> JSONResponse:
    """处理KQuery Pro自定义基类异常。
    
    将业务异常转换为标准的JSON错误响应，
    包含错误码、消息、详情和请求ID。
    
    Args:
        request: HTTP请求对象
        exc: KQueryBaseException实例
        
    Returns:
        标准格式的JSON错误响应
    """
    # 获取请求ID（如果存在）
    request_id = getattr(request.state, "request_id", None)
    
    # 构建响应内容
    response_content: Dict[str, Any] = exc.to_dict()
    
    if request_id:
        response_content["request_id"] = request_id
    
    # 记录日志（根据状态码选择级别）
    if exc.status_code >= 500:
        logger.error(
            f"Server error: {exc.code.value} - {exc.message}",
            extra={"request_id": request_id, "details": exc.details},
            exc_info=True,
        )
    elif exc.status_code >= 400:
        logger.warning(
            f"Client error: {exc.code.value} - {exc.message}",
            extra={"request_id": request_id, "path": request.url.path},
        )
    
    return JSONResponse(
        status_code=exc.status_code,
        content=response_content,
    )


async def rate_limit_error_handler(
    request: Request,
    exc: RateLimitError,
) -> JSONResponse:
    """处理速率限制异常。
    
    添加Retry-After头，告知客户端何时可以重试。
    
    Args:
        request: HTTP请求对象
        exc: RateLimitError实例
        
    Returns:
        429状态码的JSON响应
    """
    retry_after = exc.details.get("retry_after", 60)
    
    return JSONResponse(
        status_code=429,
        content=exc.to_dict(),
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": "100",
            "X-Request-ID": getattr(request.state, "request_id", ""),
        },
    )


async def validation_error_handler(
    request: Request,
    exc: ValidationError,
) -> JSONResponse:
    """处理输入验证异常。
    
    返回详细的字段级错误信息。
    
    Args:
        request: HTTP请求对象
        exc: ValidationError实例
        
    Returns:
        422状态码的JSON响应
    """
    return JSONResponse(
        status_code=422,
        content=exc.to_dict(),
        headers={"X-Request-ID": getattr(request.state, "request_id", "")},
    )


async def resource_not_found_handler(
    request: Request,
    exc: ResourceNotFoundError,
) -> JSONResponse:
    """处理资源未找到异常。
    
    Args:
        request: HTTP请求对象
        exc: ResourceNotFoundError实例
        
    Returns:
        404状态码的JSON响应
    """
    return JSONResponse(
        status_code=404,
        content=exc.to_dict(),
        headers={"X-Request-ID": getattr(request.state, "request_id", "")},
    )


async def http_exception_handler(
    request: Request,
    exc: HTTPException,
) -> JSONResponse:
    """处理Starlette/FastAPI内置HTTP异常。
    
    将标准HTTP异常转换为统一格式。
    
    Args:
        request: HTTP请求对象
        exc: HTTPException实例
        
    Returns:
        对应状态码的JSON响应
    """
    from app.core.exceptions import ErrorCode
    
    # 映射标准HTTP状态码到错误码
    code_mapping = {
        400: ErrorCode.VAL_INVALID_FORMAT,
        401: ErrorCode.SYS_INTERNAL_ERROR,
        403: ErrorCode.SYS_INTERNAL_ERROR,
        404: ErrorCode.RES_NOT_FOUND,
        405: ErrorCode.VAL_INVALID_FORMAT,
        409: ErrorCode.RES_ALREADY_EXISTS,
        413: ErrorCode.VAL_FILE_TOO_LARGE,
        415: ErrorCode.VAL_FILE_TYPE_NOT_ALLOWED,
        422: ErrorCode.VAL_REQUIRED_FIELD,
        429: ErrorCode.RATE_LIMIT_EXCEEDED,
        500: ErrorCode.SYS_INTERNAL_ERROR,
        502: ErrorCode.EXT_SERVICE_UNAVAILABLE,
        503: ErrorCode.EXT_SERVICE_UNAVAILABLE,
        504: ErrorCode.EXT_SERVICE_TIMEOUT,
    }
    
    code = code_mapping.get(exc.status_code, ErrorCode.SYS_INTERNAL_ERROR)
    request_id = getattr(request.state, "request_id", None)
    
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": code.value,
                "message": exc.detail or "HTTP Error",
                "status": exc.status_code,
            },
            **({"request_id": request_id} if request_id else {}),
        },
    )


async def pydantic_validation_error_handler(
    request: Request,
    exc: PydanticValidationError,
) -> JSONResponse:
    """处理Pydantic模型验证错误。
    
    提取字段级错误详情，返回结构化信息。
    
    Args:
        request: HTTP请求对象
        exc: PydanticValidationError实例
        
    Returns:
        422状态码的JSON响应（包含字段级错误）
    """
    errors = []
    
    for error in exc.errors():
        field = ".".join(str(loc) for loc in error["loc"])
        
        errors.append({
            "field": field if field else "__root__",
            "message": error["msg"],
            "type": error["type"],
        })
    
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VAL_002",  # INVALID_FORMAT
                "message": "数据验证失败",
                "details": {"errors": errors},
            },
            "request_id": getattr(request.state, "request_id", ""),
        },
    )


async def unhandled_exception_handler(
    request: Request,
    exc: Exception,
) -> JSONResponse:
    """处理所有未预期的异常。
    
    这是最后的防线，确保任何异常都返回友好的响应。
    生产环境中不暴露具体错误细节。
    
    Args:
        request: HTTP请求对象
        exc: 未预期的异常实例
        
    Returns:
        500状态码的JSON响应
    """
    request_id = getattr(request.state, "request_id", None)
    
    # 详细记录到日志（包含完整堆栈）
    logger.exception(
        f"Unhandled exception: {type(exc).__name__}: {str(exc)}",
        extra={
            "request_id": request_id,
            "path": request.url.path,
            "method": request.method,
        },
    )
    
    # 构建安全响应（不泄露内部信息）
    from app.core.settings import get_settings
    
    settings = get_settings()
    
    response = {
        "error": {
            "code": "SYS_001",
            "message": "服务器内部错误，请稍后重试",
        },
    }
    
    if request_id:
        response["request_id"] = request_id
    
    # 仅在调试模式下返回详细信息
    if settings.debug:
        response["debug"] = {
            "exception_type": type(exc).__name__,
            "exception_message": str(exc)[:500],
        }
    
    return JSONResponse(status_code=500, content=response)


def register_exception_handlers(app: FastAPI) -> None:
    """注册所有全局异常处理器。
    
    应在应用启动时调用一次。
    
    注册顺序（从特殊到一般）：
    1. 自定义业务异常（最具体的先注册）
    2. Pydantic验证错误
    3. Starlette HTTP异常
    4. 兜底处理器（最后注册）
    
    Args:
        app: FastAPI应用实例
        
    Example:
        >>> from fastapi import FastAPI
        >>> app = FastAPI()
        >>> register_exception_handlers(app)
    """
    # ========== 自定义业务异常 ==========
    app.add_exception_handler(KQueryBaseException, kquery_base_exception_handler)

    # 特殊处理某些异常（可添加额外的头部或逻辑）
    app.add_exception_handler(RateLimitError, rate_limit_error_handler)
    app.add_exception_handler(ValidationError, validation_error_handler)
    app.add_exception_handler(ResourceNotFoundError, resource_not_found_handler)
    app.add_exception_handler(ExternalServiceError, kquery_base_exception_handler)

    
    # ========== 第三方库异常 ==========
    app.add_exception_handler(PydanticValidationError, pydantic_validation_error_handler)
    
    # ========== 内置HTTP异常 ==========
    app.add_exception_handler(HTTPException, http_exception_handler)
    
    # ========== 兜底异常处理器 ==========
    # 注意：这个处理器会捕获所有其他未处理的异常
    app.add_exception_handler(Exception, unhandled_exception_handler)
    
    logger.info("All exception handlers registered successfully")


# ========== 导出列表 ==========
__all__ = [
    "register_exception_handlers",
]
