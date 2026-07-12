"""KQuery Pro 中间件组件。

提供HTTP请求处理中间件，包括：
- 请求日志记录
- 请求ID追踪
- 请求计时
- CORS处理
- 错误处理

遵循CODE_STANDARDS.md 1.7节（中间件规范）。

使用示例：
    from fastapi import FastAPI
    from app.core.middleware import add_middleware_to_app
    
    app = FastAPI()
    add_middleware_to_app(app)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.exceptions import KQueryBaseException, handle_exception
from app.core.logger import get_logger, setup_logging

logger = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """请求日志记录中间件。
    
    记录每个HTTP请求的详细信息，包括：
    - 请求方法、路径、参数
    - 请求头摘要（敏感信息已脱敏）
    - 响应状态码
    - 处理耗时
    - 请求ID（用于追踪）
    
    日志级别规则：
    - INFO: 成功的请求（状态码<400）
    - WARNING: 客户端错误（4xx）或慢请求(>2s)
    - ERROR: 服务端错误（5xx）
    
    Args:
        app: ASGI应用实例
        skip_paths: 要跳过日志记录的路径前缀列表
        slow_threshold_ms: 慢请求阈值（毫秒）
    """
    
    def __init__(
        self,
        app: Callable,
        *,
        skip_paths: Optional[list[str]] = None,
        slow_threshold_ms: float = 2000.0,
    ) -> None:
        """初始化日志中间件。
        
        Args:
            app: ASGI应用
            skip_paths: 跳过的路径（如 /health, /metrics）
            slow_threshold_ms: 慢请求阈值(ms)
        """
        super().__init__(app)
        self.skip_paths = set(skip_paths or ["/health", "/metrics"])
        self.slow_threshold_ms = slow_threshold_ms
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """处理请求，记录日志。
        
        Args:
            request: HTTP请求对象
            call_next: 下一个处理器
            
        Returns:
            HTTP响应对象
        """
        # 跳过指定路径
        if any(request.url.path.startswith(p) for p in self.skip_paths):
            return await call_next(request)
        
        # 生成或获取请求ID
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        
        # 记录开始时间
        start_time = time.perf_counter()
        
        # 记录请求信息
        extra = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "client_ip": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent", "")[:200],
            # ELK 可读字段
            "log_category": "access",
            "http_method": request.method,
            "url_path": request.url.path,
            "query_string": str(request.query_params) if request.query_params else None,
        }
        
        logger.info(f"Request started: {request.method} {request.url.path}", extra=extra)
        
        try:
            # 处理请求
            response = await call_next(request)
            
            # 计算耗时
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            # 设置响应头
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Response-Time"] = f"{duration_ms:.2f}ms"
            
            # 记录响应信息
            response_extra = {
                **extra,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
                # ELK 可读字段
                "http_status": response.status_code,
                "response_time_ms": round(duration_ms, 2),
                "is_slow": duration_ms > self.slow_threshold_ms,
            }
            
            # 根据状态码选择日志级别
            if response.status_code >= 500:
                logger.error(
                    f"Request failed: {response.status_code} {duration_ms:.2f}ms",
                    extra=response_extra,
                )
            elif response.status_code >= 400 or duration_ms > self.slow_threshold_ms:
                logger.warning(
                    f"Request warning: {response.status_code} {duration_ms:.2f}ms",
                    extra=response_extra,
                )
            else:
                logger.info(
                    f"Request completed: {response.status_code} {duration_ms:.2f}ms",
                    extra=response_extra,
                )
            
            return response
            
        except Exception as exc:
            # 处理异常
            duration_ms = (time.perf_counter() - start_time) * 1000
            
            error_extra = {
                **extra,
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:200],
                "duration_ms": round(duration_ms, 2),
            }
            
            logger.exception(
                f"Request exception: {type(exc).__name__}",
                extra=error_extra,
            )
            
            # 返回标准错误响应
            error_response = handle_exception(exc)
            error_response["request_id"] = request_id
            
            return JSONResponse(
                status_code=getattr(exc, "status_code", 500),
                content=error_response,
            )


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """全局错误处理中间件。
    
    捕获所有未处理的异常，转换为统一的JSON格式响应。
    确保即使发生未预期错误，也能返回友好的错误信息。
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """处理请求，捕获异常。
        
        Args:
            request: HTTP请求
            call_next: 下一个处理器
            
        Returns:
            HTTP响应
        """
        try:
            return await call_next(request)
            
        except KQueryBaseException as exc:
            # 已知的业务异常
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.to_dict(),
                headers={"X-Request-ID": getattr(request.state, "request_id", "")},
            )
            
        except Exception as exc:
            # 未预期的异常
            logger.exception("Unhandled exception occurred")
            
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "code": "SYS_001",
                        "message": "服务器内部错误，请稍后重试",
                        "details": {},
                    },
                    "request_id": getattr(request.state, "request_id", ""),
                },
            )


def configure_cors(app: object, settings: object) -> None:
    """配置CORS跨域策略。
    
    Args:
        app: FastAPI应用实例
        settings: 应用配置对象
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time"],
    )


def add_middleware_to_app(app: object) -> None:
    """向FastAPI应用添加所有中间件。
    
    应在应用启动时调用一次。
    
    中间件添加顺序（从外到内）：
    1. CORS中间件
    2. 错误处理中间件
    3. 请求日志中间件
    
    Args:
        app: FastAPI应用实例
        
    Example:
        >>> from fastapi import FastAPI
        >>> app = FastAPI()
        >>> add_middleware_to_app(app)
    """
    from app.core.settings import get_settings
    
    settings = get_settings()
    
    # 1. CORS
    configure_cors(app, settings)
    
    # 2. 错误处理
    app.add_middleware(ErrorHandlingMiddleware)
    
    # 3. 请求日志
    app.add_middleware(
        RequestLoggingMiddleware,
        slow_threshold_ms=settings.process_timeout * 1000 * 0.5,  # 50%超时作为慢请求阈值
    )


# ========== 导出列表 ==========
__all__ = [
    "RequestLoggingMiddleware",
    "ErrorHandlingMiddleware",
    "configure_cors",
    "add_middleware_to_app",
]
