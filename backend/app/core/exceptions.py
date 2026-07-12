"""KQuery Pro 异常类体系。

定义所有自定义异常类型，用于统一的错误处理和响应。
遵循CODE_STANDARDS.md 1.8节（异常处理规范）。

设计原则：
1. 所有业务异常继承自KQueryBaseException
2. 每个异常包含错误码、消息、详情
3. 支持链式异常（__cause__）
4. 可序列化为JSON响应格式

使用示例：
    from app.core.exceptions import (
        ValidationError,
        ResourceNotFoundError,
        raise_if,
        handle_exception,
    )
    
    # 方式1：直接抛出
    raise ValidationError("字段不能为空", field="name")
    
    # 方式2：条件抛出
    raise_if(not doc, ResourceNotFoundError("文档不存在"), doc_id=id)
    
    # 方式3：在API处理器中使用
    try:
        result = service.process(data)
    except KQueryBaseException as e:
        return handle_exception(e)  # 返回统一JSON格式
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Type


class ErrorCode(str, Enum):
    """系统错误码定义。
    
    格式：MODULE_SPECIFIC_ERROR（模块_具体错误）
    
    Attributes:
        VALIDATION: 输入验证错误
        RESOURCE: 资源未找到或冲突
        RATE_LIMIT: 速率限制错误
        EXTERNAL: 外部服务调用错误
        INTERNAL: 系统内部错误
    """
    # 输入验证 (VAL_xxx)
    VAL_REQUIRED_FIELD = "VAL_001"                  # 必填字段缺失
    VAL_INVALID_FORMAT = "VAL_002"                  # 格式无效
    VAL_OUT_OF_RANGE = "VAL_003"                    # 值超出范围
    VAL_TOO_LONG = "VAL_004"                        # 长度超限
    VAL_INVALID_TYPE = "VAL_005"                    # 类型错误
    VAL_FILE_TOO_LARGE = "VAL_006"                  # 文件过大
    VAL_FILE_TYPE_NOT_ALLOWED = "VAL_007"          # 文件类型不允许
    VAL_MALFORMED_JSON = "VAL_008"                 # JSON格式错误
    VAL_SQL_INJECTION = "VAL_009"                   # SQL注入检测
    VAL_XSS_DETECTED = "VAL_010"                   # XSS攻击检测
    VAL_INVALID_FILE_CONTENT = "VAL_013"           # 文件内容校验失败
    # 兼容别名
    INVALID_FILE_CONTENT = "VAL_013"               # 文件内容校验失败（兼容别名）
    
    # 资源错误 (RES_xxx)
    RES_NOT_FOUND = "RES_001"                       # 资源不存在
    RES_ALREADY_EXISTS = "RES_002"                  # 资源已存在
    RES_CONFLICT = "RES_003"                        # 资源冲突
    RES_DELETED = "RES_004"                         # 资源已删除
    RES_IN_USE = "RES_005"                          # 资源正在使用
    
    # 业务逻辑 (BIZ_xxx)
    BIZ_PROCESS_FAILED = "BIZ_001"                  # 处理失败
    BIZ_INVALID_STATE = "BIZ_002"                   # 无效状态
    BIZ_RULE_VIOLATED = "BIZ_003"                  # 违反业务规则
    BIZ_QUOTA_EXCEEDED = "BIZ_004"                 # 配额超限
    
    # 外部服务 (EXT_xxx)
    EXT_SERVICE_UNAVAILABLE = "EXT_001"            # 服务不可用
    EXT_SERVICE_TIMEOUT = "EXT_002"               # 服务超时
    EXT_RATE_LIMITED = "EXT_003"                   # 外部服务限流
    EXT_INVALID_RESPONSE = "EXT_004"              # 无效响应
    EXT_CONNECTION_FAILED = "EXT_005"             # 连接失败
    
    # 系统内部 (SYS_xxx)
    SYS_INTERNAL_ERROR = "SYS_001"                 # 内部错误
    SYS_CONFIGURATION_ERROR = "SYS_002"           # 配置错误
    SYS_DEPENDENCY_MISSING = "SYS_003"            # 依赖缺失
    SYS_DATABASE_ERROR = "SYS_004"                # 数据库错误
    SYS_CACHE_ERROR = "SYS_005"                   # 缓存错误
    
    # 速率限制 (RATE_xxx)
    RATE_LIMIT_EXCEEDED = "RATE_001"              # 请求过于频繁
    RATE_DAILY_LIMIT = "RATE_002"                 # 每日配额用尽
    RATE_API_LIMIT = "RATE_003"                   # API调用限制


class KQueryBaseException(Exception):
    """基础异常类。
    
    所有KQuery Pro的自定义异常都应继承此类。
    提供统一的错误信息结构，便于日志记录和API响应。
    
    Args:
        message: 人类可读的错误信息
        code: 错误码（默认为SYS_INTERNAL_ERROR）
        details: 附加的错误详细信息（可选）
        status_code: HTTP状态码（默认500）
        
    Attributes:
        message: 错误信息
        code: 错误码
        details: 详细信息字典
        status_code: HTTP状态码
        
    Example:
        >>> raise KQueryBaseException(
        ...     "处理失败",
        ...     code=ErrorCode.BIZ_PROCESS_FAILED,
        ...     details={"step": "embedding", "doc_id": 123}
        ... )
        KQueryBaseException: 处理失败 [BIZ_001]
    """
    
    def __init__(
        self,
        message: str,
        code: ErrorCode = ErrorCode.SYS_INTERNAL_ERROR,
        details: Optional[Dict[str, Any]] = None,
        status_code: int = 500,
    ) -> None:
        """初始化异常实例。
        
        Args:
            message: 人类可读的错误信息
            code: 错误码枚举值
            details: 附加的详细错误信息
            status_code: 对应的HTTP状态码
        """
        super().__init__(message)
        self.message: str = message
        self.code: ErrorCode = code
        self.details: Dict[str, Any] = details or {}
        self.status_code: int = status_code
    
    def to_dict(self) -> Dict[str, Any]:
        """将异常转换为字典格式，用于JSON序列化。
        
        Returns:
            包含错误信息的字典
            
        Example:
            >>> exc = ValidationError("字段必填", field="name")
            >>> exc.to_dict()
            {
                'error': {
                    'code': 'VAL_001',
                    'message': '字段必填',
                    'details': {'field': 'name'}
                }
            }
        """
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details if self.details else None,
            }
        }
    
    def __str__(self) -> str:
        """返回可读的异常描述。"""
        return f"{self.message} [{self.code.value}]"
    
    def __repr__(self) -> str:
        """返回详细的异常表示。"""
        return (
            f"{self.__class__.__name__}("
            f"message={self.message!r}, "
            f"code={self.code!r}, "
            f"status_code={self.status_code})"
        )


# ========== 输入验证异常 ==========
class ValidationError(KQueryBaseException):
    """输入验证异常。
    
    当请求数据不符合预期格式、约束或业务规则时抛出。
    
    Args:
        message: 错误信息（默认："数据验证失败"）
        field: 验证失败的字段名
        value: 导致失败的值（敏感数据会被脱敏）
    """
    
    def __init__(
        self,
        message: str = "数据验证失败",
        field: Optional[str] = None,
        value: Optional[Any] = None,
        code: ErrorCode = ErrorCode.VAL_REQUIRED_FIELD,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            status_code=422,
            **kwargs,
        )
        if field:
            self.details["field"] = field
        if value is not None:
            # 脱敏敏感值
            self.details["value"] = self._sanitize_value(value)
    
    @staticmethod
    def _sanitize_value(value: Any) -> str:
        """对敏感值进行脱敏处理。
        
        Args:
            value: 原始值
                
        Returns:
            脱敏后的字符串表示
        """
        value_str = str(value)
        if len(value_str) > 50:
            return f"{value_str[:20]}...{value_str[-10:]}"
        return value_str


class FileValidationError(ValidationError):
    """文件验证异常。
    
    当上传的文件不符合要求（大小、类型等）时抛出。
    """
    
    def __init__(
        self,
        message: str = "文件验证失败",
        filename: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            **kwargs,
        )
        if filename:
            self.details["filename"] = filename


# ========== 资源异常 ==========
class ResourceNotFoundError(KQueryBaseException):
    """资源未找到异常。
    
    当请求的资源（文档、知识库、用户等）不存在时抛出。
    
    Args:
        message: 错误信息（默认："资源未找到"）
        resource_type: 资源类型（如"Document"、"User"）
        resource_id: 资源标识符
    """
    
    def __init__(
        self,
        message: str = "资源未找到",
        resource_type: Optional[str] = None,
        resource_id: Optional[Any] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=ErrorCode.RES_NOT_FOUND,
            status_code=404,
            **kwargs,
        )
        if resource_type:
            self.details["resource_type"] = resource_type
        if resource_id is not None:
            self.details["resource_id"] = str(resource_id)


class ResourceConflictError(KQueryBaseException):
    """资源冲突异常。
    
    当操作与已有资源冲突（如重复创建）时抛出。
    """
    
    def __init__(
        self,
        message: str = "资源冲突",
        resource_type: Optional[str] = None,
        code: ErrorCode = ErrorCode.RES_ALREADY_EXISTS,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            status_code=409,
            **kwargs,
        )
        if resource_type:
            self.details["resource_type"] = resource_type


# ========== 业务逻辑异常 ==========
class BusinessRuleViolationError(KQueryBaseException):
    """业务规则违反异常。
    
    当操作违反特定业务规则时抛出。
    """
    
    def __init__(
        self,
        message: str = "违反业务规则",
        rule_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=ErrorCode.BIZ_RULE_VIOLATED,
            status_code=422,
            **kwargs,
        )
        if rule_name:
            self.details["rule"] = rule_name


# ========== 外部服务异常 ==========
class ExternalServiceError(KQueryBaseException):
    """外部服务调用异常。
    
    当调用外部服务（阿里云、讯飞、Milvus等）失败时抛出。
    """
    
    def __init__(
        self,
        message: str = "外部服务调用失败",
        service_name: Optional[str] = None,
        original_error: Optional[Exception] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=ErrorCode.EXT_SERVICE_UNAVAILABLE,
            status_code=502,
            **kwargs,
        )
        if service_name:
            self.details["service"] = service_name
        if original_error:
            self.details["original_error"] = str(original_error)


class ServiceTimeoutError(ExternalServiceError):
    """外部服务超时异常。
    """
    
    def __init__(
        self,
        message: str = "服务调用超时",
        timeout_seconds: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=ErrorCode.EXT_SERVICE_TIMEOUT,
            **kwargs,
        )
        if timeout_seconds:
            self.details["timeout_seconds"] = timeout_seconds


# ========== 速率限制异常 ==========
class RateLimitError(KQueryBaseException):
    """速率限制异常。
    
    当请求频率超过限制时抛出。
    """
    
    def __init__(
        self,
        message: str = "请求过于频繁，请稍后重试",
        retry_after: Optional[int] = None,
        limit_type: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=ErrorCode.RATE_LIMIT_EXCEEDED,
            status_code=429,
            **kwargs,
        )
        if retry_after is not None:
            self.details["retry_after"] = retry_after
        if limit_type:
            self.details["limit_type"] = limit_type


# ========== 工具函数 ==========
def raise_if(
    condition: bool,
    exception: Exception,
    **context: Any,
) -> None:
    """条件性抛出异常。
    
    当condition为True时抛出指定异常，
    并将上下文信息添加到异常的details中。
    
    Args:
        condition: 判断条件（为True时抛出）
        exception: 要抛出的异常实例
        **context: 要添加到details中的上下文信息
        
    Raises:
        exception: 当condition为True时
        
    Example:
        >>> user = None
        >>> raise_if(user is None, ResourceNotFoundError("用户不存在"), id=123)
        Traceback (most recent call last):
            ...
        ResourceNotFoundError: 用户不存在 [RES_001]
    """
    if condition:
        if context and hasattr(exception, "details"):
            exception.details.update(context)
        raise exception


def handle_exception(exc: Exception) -> Dict[str, Any]:
    """将异常转换为标准的API响应格式。
    
    用于FastAPI异常处理器中，确保所有异常都返回统一格式。
    
    Args:
        exc: 异常实例
        
    Returns:
        标准格式的错误响应字典
        
    Example:
        >>> try:
        ...     process_user(None)
        ... except Exception as e:
        ...     response = handle_exception(e)
        {'error': {'code': 'RES_001', 'message': '用户不能为空', 'details': {}}}
    """
    if isinstance(exc, KQueryBaseException):
        return exc.to_dict()
    
    # 处理非KQuery异常
    import logging
    logger = logging.getLogger(__name__)
    logger.exception("Unhandled exception occurred")
    
    return {
        "error": {
            "code": ErrorCode.SYS_INTERNAL_ERROR.value,
            "message": "服务器内部错误，请联系管理员",
            "details": {"type": type(exc).__name__},
        }
    }


# ========== 导出列表 ==========
__all__ = [
    # 错误码
    "ErrorCode",
    # 基础异常
    "KQueryBaseException",
    # 验证异常
    "ValidationError",
    "FileValidationError",
    # 资源异常
    "ResourceNotFoundError",
    "ResourceConflictError",
    # 业务异常
    "BusinessRuleViolationError",
    # 外部服务异常
    "ExternalServiceError",
    "ServiceTimeoutError",
    # 速率限制异常
    "RateLimitError",
    # 工具函数
    "raise_if",
    "handle_exception",
]
