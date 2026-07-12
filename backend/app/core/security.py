"""
安全工具模块

提供输入验证、清理和安全防护功能
"""

import re
import os
from pathlib import Path
from typing import Optional, List, Set, Tuple
from fastapi import UploadFile
from pydantic import validator, field_validator

# SQL注入检测模式
SQL_INJECTION_PATTERNS = [
    r"(\b(UNION|SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|EXEC|EXECUTE)\b)",
    r"(--|\#)",  # SQL注释
    r"(;)\s*(\b(SELECT|INSERT|UPDATE|DELETE|DROP)\b)",  # 多语句
    r"('\s*(OR|AND)\s*')",  # ' OR '1'='1'
    r"('\s*=\s*')",
]

# XSS检测模式
XSS_PATTERNS = [
    r"<\s*script[^>]*>.*?<\s*/\s*script\s*>",  # <script>...</script>
    r"<\s*iframe[^>]*>",  # <iframe>
    r"javascript\s*:",  # javascript:
    r"on(error|load|click|mouseover)\s*=",  # 事件处理器
    r"<\s*img[^>]+onerror\s*=",  # img onerror
]


class InputValidator:
    """
    输入验证器
    
    提供各种输入验证和安全检查功能
    
    使用示例:
        validator = InputValidator()
        
        if not validator.is_safe_string(user_input):
            raise ValidationError("非法字符")
            
        clean_filename = validator.sanitize_filename(upload.filename)
    """
    
    def __init__(self):
        """初始化验证器，编译正则表达式"""
        self.sql_patterns = [re.compile(p, re.IGNORECASE) for p in SQL_INJECTION_PATTERNS]
        self.xss_patterns = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in XSS_PATTERNS]
    
    def is_safe_string(self, value: str) -> bool:
        """
        检查字符串是否安全（无SQL注入或XSS攻击）
        
        Args:
            value: 要检查的字符串
            
        Returns:
            True表示安全，False表示可能包含恶意内容
        """
        if not value or not isinstance(value, str):
            return True
        
        for pattern in self.sql_patterns + self.xss_patterns:
            if pattern.search(value):
                return False
        
        return True
    
    def sanitize_html(self, value: str) -> str:
        """
        清理HTML标签，防止XSS攻击
        
        转义危险的HTML特殊字符
        
        Args:
            value: 可能包含HTML的字符串
            
        Returns:
            清理后的安全字符串
        """
        if not isinstance(value, str):
            return str(value)
        
        replacements = {
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#x27;',
            '/': '&#x2F;',  # 防止 </script>
        }
        
        result = value
        for char, escaped in replacements.items():
            result = result.replace(char, escaped)
        
        return result
    
    def sanitize_filename(self, filename: str) -> Optional[str]:
        """
        清理文件名，防止路径穿越攻击
        
        移除或替换危险字符：
        - 路径分隔符 (/ \\)
        - 特殊字符 (.. | : * ? " < >)
        
        Args:
            filename: 原始文件名
            
        Returns:
            安全的文件名，如果无法清理则返回None
            
        示例:
            >>> v = InputValidator()
            >>> v.sanitize_filename("../../../etc/passwd")
            None
            >>> v.sanitize_filename("document.pdf")
            "document.pdf"
        """
        if not filename:
            return None
        
        # 移除路径信息，只保留文件名
        clean_name = os.path.basename(filename)
        
        # 检查是否有路径穿越尝试
        if ".." in filename or "/" in filename or "\\" in filename:
            from app.core.exceptions import ValidationError, ErrorCode
            raise ValidationError(
                message="文件名包含非法路径字符",
                code=ErrorCode.VAL_INVALID_FORMAT
            )
        
        # 替换危险字符
        dangerous_chars = ['<', '>', ':', '"', '|', '?', '*']
        for char in dangerous_chars:
            clean_name = clean_name.replace(char, '_')
        
        # 检查长度
        if len(clean_name) > 255:
            clean_name = clean_name[:255]
        
        # 确保不为空且不只有点号
        if not clean_name.strip('.') or len(clean_name.strip('.')) == 0:
            return None
        
        return clean_name
    
    def validate_file_type(
        self,
        filename: str,
        allowed_extensions: Set[str]
    ) -> bool:
        """
        验证文件类型是否在允许列表中
        
        Args:
            filename: 文件名
            allowed_extensions: 允许的扩展名集合 {".pdf", ".docx"}
            
        Returns:
            是否允许该文件类型
        """
        if not allowed_extensions:
            return True  # 未设置限制则允许所有
        
        ext = Path(filename).suffix.lower()
        return ext in allowed_extensions
    
    def validate_query_parameter(
        self,
        param_name: str,
        param_value: str,
        max_length: int = 1000
    ) -> str:
        """
        验证并清理查询参数
        
        Args:
            param_name: 参数名称
            param_value: 参数值
            max_length: 最大允许长度
            
        Returns:
            清理后的参数值
            
        Raises:
            ValidationError: 参数不合法
        """
        if param_value is None:
            return ""
        
        if not isinstance(param_value, str):
            param_value = str(param_value)
        
        # 长度检查
        if len(param_value) > max_length:
            from app.core.exceptions import ValidationError, ErrorCode
            raise ValidationError(
                message=f"参数 '{param_name}' 超过最大长度限制 ({max_length})",
                code=ErrorCode.VALIDATION_ERROR
            )
        
        # SQL注入检查
        for pattern in self.sql_patterns:
            if pattern.search(param_value):
                from app.core.exceptions import ValidationError, ErrorCode
                raise ValidationError(
                    message=f"参数 '{param_name}' 包含非法字符",
                    code=ErrorCode.SQL_INJECTION_DETECTED
                )
        
        return param_value


# 全局实例
input_validator = InputValidator()


# ==================== Pydantic自定义验证器 ====================

@field_validator('username')
def validate_username(cls, v: str) -> str:
    """
    Pydantic验证器：验证用户名格式
    只允许字母、数字、下划线，3-50个字符
    """
    if not re.match(r'^[a-zA-Z0-9_]{3,50}$', v):
        raise ValueError('用户名只能包含字母、数字、下划线，长度3-50位')
    return v


@field_validator('query_text')
def validate_query_text(cls, v: str) -> str:
    """
    Pydantic验证器：验证查询文本安全性
    检查SQL注入和XSS
    """
    if not input_validator.is_safe_string(v):
        raise ValueError('查询文本包含非法内容')
    if len(v) > 10000:
        raise ValueError('查询文本过长，最大10000字符')
    return input_validator.sanitize_html(v)


@field_validator('filename')
def validate_upload_filename(cls, v: str) -> str:
    """
    Pydantic验证器：验证上传文件名安全性
    """
    clean_name = input_validator.sanitize_filename(v)
    if not clean_name:
        raise ValueError('文件名无效')
    return clean_name


class SecurityHeaders:
    """
    安全响应头管理器
    
    设置HTTP安全响应头，防止常见Web攻击
    """
    
    HEADERS = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Content-Security-Policy": "default-src 'self'",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()"
    }
    
    @classmethod
    def get_headers(cls) -> dict:
        """获取所有安全头"""
        return cls.HEADERS.copy()


def add_security_headers_middleware(app):
    """
    为FastAPI应用添加安全响应头中间件
    
    Args:
        app: FastAPI应用实例
    """
    @app.middleware("http")
    async def add_security_headers(request, call_next):
        """中间件函数：为每个响应添加安全头"""
        response = await call_next(request)
        
        for header, value in SecurityHeaders.HEADERS.items():
            response.headers[header] = value
        
        return response


# ==================== 文件上传安全检查（v2.1 新增）====================

# 文件扩展名白名单
UPLOAD_ALLOWED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".txt", ".md", ".html",
    ".xlsx", ".xls", ".pptx", ".ppt", ".csv", ".xml", ".json",
}

# 最大上传文件大小（50MB）
UPLOAD_MAX_FILE_SIZE = 50 * 1024 * 1024

# Magic Number 映射表（用于校验文件内容真实性）
MAGIC_NUMBER_MAP = {
    ".pdf": [b"%PDF"],
    ".docx": [b"PK\x03\x04"],  # ZIP格式（Office Open XML）
    ".xlsx": [b"PK\x03\x04"],
    ".pptx": [b"PK\x03\x04"],
    ".doc": [b"\xd0\xcf\x11\xe0"],  # OLE2格式
    ".xls": [b"\xd0\xcf\x11\xe0"],
    ".ppt": [b"\xd0\xcf\x11\xe0"],
    ".txt": [],  # 文本文件无固定magic number
    ".md": [],
    ".html": [],
    ".csv": [],
    ".xml": [],
    ".json": [],
}

# MIME 类型映射
EXTENSION_TO_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".html": "text/html",
    ".csv": "text/csv",
    ".xml": "application/xml",
    ".json": "application/json",
}


async def validate_upload_file(
    file: UploadFile,
    allowed_extensions: Optional[Set[str]] = None,
    max_file_size: Optional[int] = None,
) -> Tuple[bytes, str]:
    """集中式文件上传安全验证。
    
    执行多层安全检查：
    1. 文件名路径穿越清理
    2. 扩展名白名单校验
    3. MIME 类型与扩展名一致性校验
    4. Magic Number 内容真实性校验
    5. 文件大小限制
    
    Args:
        file: FastAPI UploadFile 对象
        allowed_extensions: 允许的扩展名集合（默认使用全局配置）
        max_file_size: 最大文件大小字节数（默认50MB）
        
    Returns:
        (文件内容字节, 安全文件名) 元组
        
    Raises:
        ValidationError: 任何安全检查失败时抛出
    """
    from app.core.exceptions import ValidationError, ErrorCode
    
    extensions = allowed_extensions or UPLOAD_ALLOWED_EXTENSIONS
    max_size = max_file_size or UPLOAD_MAX_FILE_SIZE
    
    # 1. 检查文件名是否存在
    if not file.filename:
        raise ValidationError(
            message="文件名不能为空",
            code=ErrorCode.VAL_INVALID_FORMAT
        )
    
    # 2. 清理文件名，防止路径穿越
    safe_filename = input_validator.sanitize_filename(file.filename)
    if not safe_filename:
        raise ValidationError(
            message="文件名无效或包含非法字符",
            code=ErrorCode.VAL_INVALID_FORMAT
        )
    
    # 3. 检查文件扩展名
    ext = Path(safe_filename).suffix.lower()
    if ext not in extensions:
        raise ValidationError(
            message=f"不支持的文件类型: {ext}。支持的类型: {', '.join(sorted(extensions))}",
            code=ErrorCode.VAL_FILE_TYPE_NOT_ALLOWED
        )
    
    # 4. 读取文件内容
    content = await file.read()
    
    # 5. 检查文件大小
    if len(content) > max_size:
        raise ValidationError(
            message=f"文件大小超过限制: {len(content) / 1024 / 1024:.1f}MB > {max_size / 1024 / 1024:.0f}MB",
            code=ErrorCode.VAL_FILE_TOO_LARGE
        )
    
    # 6. 检查空文件
    if len(content) == 0:
        raise ValidationError(
            message="文件内容为空",
            code=ErrorCode.VAL_INVALID_FORMAT
        )
    
    # 7. MIME 类型与扩展名一致性校验（如果提供了 content_type）
    if file.content_type:
        expected_mime = EXTENSION_TO_MIME.get(ext)
        if expected_mime and file.content_type != expected_mime:
            # 允许宽松匹配：application/octet-stream（curl默认上传类型）或 text/*（纯文本文件）
            if not (
                file.content_type == "application/octet-stream"
                or (
                    ext in {".txt", ".md", ".csv", ".json", ".xml"}
                    and file.content_type.startswith("text/")
                )
            ):
                raise ValidationError(
                    message=f"MIME类型与扩展名不一致: {file.content_type} vs {ext}",
                    code=ErrorCode.INVALID_FILE_CONTENT
                )
    
    # 8. Magic Number 校验（防止伪造扩展名）
    magic_numbers = MAGIC_NUMBER_MAP.get(ext, [])
    if magic_numbers:
        matched = any(content.startswith(magic) for magic in magic_numbers)
        if not matched:
            raise ValidationError(
                message=f"文件内容与其扩展名 {ext} 不匹配（Magic Number校验失败）",
                code=ErrorCode.VAL_INVALID_FORMAT
            )
    
    # 重置文件指针（以便后续可以重新读取）
    await file.seek(0)
    
    return content, safe_filename
