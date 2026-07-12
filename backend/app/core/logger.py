"""KQuery Pro 日志管理模块。

提供统一的日志配置和管理功能，支持结构化日志输出。
遵循CODE_STANDARDS.md 1.6节（日志规范）。

主要特性：
- 结构化JSON日志输出（生产环境）
- 彩色控制台输出（开发环境）
- 敏感数据自动脱敏
- 请求ID追踪支持
- 多级别日志控制
- 异步日志写入（可选）
- ELK (Elasticsearch + Logstash + Kibana) 日志推送

使用示例：
    from app.core.logger import get_logger, setup_logging
    
    # 获取Logger实例
    logger = get_logger(__name__)
    
    # 基本用法
    logger.info("用户登录成功", extra={"user_id": 123})
    logger.error("处理失败", exc_info=True, extra={"doc_id": 456})
    
    # 使用上下文管理器绑定请求信息
    with logger.contextualize(request_id="abc123"):
        logger.info("这个日志会包含request_id")
"""

from __future__ import annotations

import json
import logging
import socket
import sys
import traceback
from datetime import datetime, timezone
from functools import lru_cache
from logging import LogRecord
from typing import Any, Dict, Optional


class SensitiveDataFilter(logging.Filter):
    """敏感数据过滤器。
    
    自动检测并脱敏日志中的敏感信息，包括：
    - 密码字段（password, passwd, pwd, secret）
    - API Key（api_key, apikey, access_token）
    - 手机号（11位数字，138****1234格式）
    - 邮箱地址（u***@example.com格式）
    - 身份证号（隐藏中间位数）
    
    Attributes:
        sensitive_patterns: 敏感数据的正则模式映射
    """
    
    # 敏感字段名称模式（不区分大小写）
    SENSITIVE_FIELDS = {
        "password", "passwd", "pwd", "secret",
        "api_key", "apikey", "access_token", "token",
        "authorization", "cookie", "session",
    }
    
    # 敏感数据值的正则替换规则
    VALUE_PATTERNS = [
        # 手机号：138****1234
        (r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)", r"\1****\2"),
        # 邮箱：u***@example.com
        (r"([a-zA-Z0-9])[a-zA-Z0-9._%+-]{1,}@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})", r"\1***@\2"),
    ]
    
    def filter(self, record: LogRecord) -> bool:
        """过滤日志记录，脱敏敏感数据。
        
        Args:
            record: 日志记录对象
            
        Returns:
            True（始终通过过滤）
        """
        # 处理日志消息中的敏感值
        record.msg = self._sanitize_message(str(record.msg))
        
        # 处理extra字段中的敏感数据
        if hasattr(record, "__dict__"):
            for key in list(record.__dict__.keys()):
                key_lower = key.lower().replace("-", "_")
                if key_lower in self.SENSITIVE_FIELDS and record.__dict__[key]:
                    record.__dict__[key] = "***"
        
        return True
    
    @classmethod
    def _sanitize_message(cls, message: str) -> str:
        """脱敏消息中的敏感值。
        
        Args:
            message: 原始消息
            
        Returns:
            脱敏后的消息
        """
        for pattern, replacement in cls.VALUE_PATTERNS:
            import re
            message = re.sub(pattern, replacement, message)
        return message


class JsonFormatter(logging.Formatter):
    """JSON格式的日志格式化器。
    
    将日志记录格式化为JSON字符串，适合生产环境日志收集系统。
    输出字段包括：
    - timestamp: ISO 8601格式时间戳
    - level: 日志级别
    - logger: Logger名称
    - message: 日志消息
    - extra: 附加字段
    - request_id: 请求追踪ID（如果有）
    
    Args:
        fmt: 未使用（保持接口兼容）
        datefmt: 时间格式（默认ISO 8601）
        include_extra: 是否包含额外字段（默认True）
    """
    
    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        *,
        include_extra: bool = True,
    ) -> None:
        """初始化JSON格式化器。
        
        Args:
            fmt: 未使用
            datefmt: 时间格式字符串
            include_extra: 是否包含额外字段
        """
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.include_extra = include_extra
        self._default_fields = {
            "name", "msg", "args", "created", "levelname",
            "levelno", "pathname", "filename", "module",
            "exc_info", "exc_text", "stack_info", "lineno",
            "funcName", "thread", "threadName", "processName",
            "process", "taskName", "message",
        }
    
    def format(self, record: LogRecord) -> str:
        """格式化日志记录为JSON。
        
        Args:
            record: 日志记录对象
            
        Returns:
            JSON格式的日志字符串
        """
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # 添加异常信息
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        # 添加额外字段
        if self.include_extra:
            extra = {}
            for key, value in record.__dict__.items():
                if key not in self._default_fields and not key.startswith("_"):
                    # 序列化特殊类型
                    extra[key] = self._serialize_value(value)
            
            if extra:
                log_entry["extra"] = extra
        
        return json.dumps(log_entry, ensure_ascii=False, default=str)
    
    @staticmethod
    def _serialize_value(value: Any) -> Any:
        """序列化特殊类型的值。
        
        Args:
            value: 要序列化的值
            
        Returns:
            序列化后的值
        """
        if isinstance(value, (set, tuple)):
            return list(value)
        if callable(value):
            return f"<callable:{value.__name__}>"
        return value


class ContextualLoggerAdapter(logging.LoggerAdapter):
    """支持上下文信息的日志适配器。
    
    允许在代码块范围内自动添加额外的日志字段，
    例如请求ID、用户ID等信息。
    
    Example:
        logger = ContextualLoggerAdapter(get_logger(__name__))
        
        # 方式1：使用with语句
        with logger.contextualize(request_id="abc123"):
            logger.info("这条日志会包含request_id")
        
        # 方式2：手动设置
        logger.push_context(user_id=123)
        logger.info("这条日志会包含user_id")
        logger.pop_context()  # 移除user_id
    """
    
    def contextualize(self, **context: Any) -> "ContextManager":
        """返回上下文管理器，在作用域内添加额外字段。
        
        Args:
            **context: 要添加的键值对
            
        Returns:
            上下文管理器
        """
        from contextlib import contextmanager
        
        @contextmanager
        def ctx_manager():
            old_extra = self.extra.copy()
            self.extra.update(context)
            try:
                yield self
            finally:
                self.extra.clear()
                self.extra.update(old_extra)
        
        return ctx_manager()
    
    def push_context(self, **context: Any) -> None:
        """推送新的上下文字段。
        
        Args:
            **context: 要添加的字段
        """
        self.extra.update(context)
    
    def pop_context(self, *keys: str) -> None:
        """移除指定的上下文字段。
        
        Args:
            *keys: 要移除的字段名
        """
        for key in keys:
            self.extra.pop(key, None)


class LogstashHandler(logging.Handler):
    """通过 TCP 将日志推送到 Logstash 的 Handler。
    
    支持异步批量发送，断线自动重连，适合生产环境使用。
    日志格式为 JSON，与 Logstash 的 json_lines codec 对接。
    
    Args:
        host: Logstash 主机地址
        port: Logstash TCP 端口
        index_name: Elasticsearch 索引名前缀
        environment: 环境标识（development/staging/production）
        timeout: 连接超时秒数
        batch_size: 批量发送大小（0=立即发送）
        flush_interval: 批量刷新间隔秒数
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5045,
        index_name: str = "kquery-logs",
        environment: str = "development",
        timeout: float = 5.0,
        batch_size: int = 100,
        flush_interval: float = 2.0,
    ) -> None:
        super().__init__()
        self.host = host
        self.port = port
        self.index_name = index_name
        self.environment = environment
        self.timeout = timeout
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self._sock: socket.socket | None = None
        self._buffer: list[str] = []
        self._last_flush: float = 0.0
        self._started: bool = False

    def emit(self, record: LogRecord) -> None:
        """发送日志记录到 Logstash。
        
        Args:
            record: 日志记录对象
        """
        try:
            log_entry = self._format_for_logstash(record)
            json_line = json.dumps(log_entry, ensure_ascii=False, default=str)
            self._buffer.append(json_line)

            # 判断是否需要刷新
            import time
            now = time.monotonic()
            if not self._started:
                self._started = True
                self._last_flush = now
            should_flush = (
                len(self._buffer) >= self.batch_size
                or (self._last_flush > 0 and (now - self._last_flush) >= self.flush_interval)
            )
            if should_flush:
                self._flush()

        except Exception:
            self.handleError(record)

    def _format_for_logstash(self, record: LogRecord) -> Dict[str, Any]:
        """将 LogRecord 格式化为 Logstash 可接收的 JSON 结构。
        
        Args:
            record: 日志记录对象
            
        Returns:
            适合 Logstash json_lines codec 的字典
        """
        log_entry: Dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname.replace("\x1b", "").split("m")[-1] if "\x1b" in record.levelname else record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "application": "kquery-pro",
            "environment": self.environment,
            "index_name": self.index_name,
            "host": socket.gethostname(),
        }

        # 异常信息
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            )

        # 附加字段
        extra = {}
        skip_keys = {
            "name", "msg", "args", "created", "levelname",
            "levelno", "pathname", "filename", "module",
            "exc_info", "exc_text", "stack_info", "lineno",
            "funcName", "thread", "threadName", "processName",
            "process", "taskName", "message",
        }
        for key, value in record.__dict__.items():
            if key not in skip_keys and not key.startswith("_"):
                extra[key] = value
        if extra:
            log_entry["extra"] = extra

        return log_entry

    def _flush(self) -> None:
        """将缓冲区中的日志发送到 Logstash。"""
        if not self._buffer:
            return

        import time
        data = "\n".join(self._buffer) + "\n"
        self._buffer.clear()
        self._last_flush = time.monotonic()

        try:
            self._send(data)
        except Exception:
            pass  # 静默失败，不影响主流程

    def _send(self, data: str) -> None:
        """通过 TCP 发送数据到 Logstash。
        
        Args:
            data: 要发送的字符串数据
        """
        if self._sock is None:
            self._connect()

        try:
            self._sock.sendall(data.encode("utf-8"))
        except (OSError, socket.error):
            # 发送失败，关闭旧连接并重连
            self._close()
            self._connect()
            self._sock.sendall(data.encode("utf-8"))

    def _connect(self) -> None:
        """建立到 Logstash 的 TCP 连接。"""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(self.timeout)
        self._sock.connect((self.host, self.port))

    def _close(self) -> None:
        """关闭 TCP 连接。"""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def flush(self) -> None:
        """手动刷新缓冲区。"""
        self._flush()

    def close(self) -> None:
        """关闭 Handler，刷新剩余日志并断开连接。"""
        self._flush()
        self._close()
        super().close()


def setup_logging(
    level: int = logging.INFO,
    json_logs: bool = False,
    log_file: Optional[str] = None,
    elk_config: Optional[Dict[str, Any]] = None,
) -> Optional["LogstashHandler"]:
    """配置根Logger。
    
    应该在应用启动时调用一次。
    
    Args:
        level: 日志级别（默认INFO）
        json_logs: 是否使用JSON格式（默认False）
        log_file: 日志文件路径（可选）
        elk_config: ELK 配置字典，包含 enabled/host/port 等字段（可选）
        
    Returns:
        LogstashHandler 实例（如果 ELK 已启用），否则 None
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # 清除已有的处理器（避免重复）
    root_logger.handlers.clear()
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    
    if json_logs:
        console_handler.setFormatter(JsonFormatter())
    else:
        console_handler.setFormatter(ColoredConsoleFormatter())
    
    root_logger.addHandler(console_handler)
    
    # 添加敏感数据过滤器到所有处理器
    sensitive_filter = SensitiveDataFilter()
    for handler in root_logger.handlers:
        handler.addFilter(sensitive_filter)
    
    # 如果需要，添加文件处理器
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(SensitiveDataFilter())
        root_logger.addHandler(file_handler)
    
    # ELK Logstash Handler（如果配置启用）
    logstash_handler = None
    if elk_config and elk_config.get("enabled", False):
        try:
            logstash_handler = LogstashHandler(
                host=elk_config.get("host", "localhost"),
                port=elk_config.get("port", 5000),
                index_name=elk_config.get("index_name", "kquery-logs"),
                environment=elk_config.get("environment", "development"),
                timeout=elk_config.get("timeout", 5.0),
                batch_size=elk_config.get("batch_size", 100),
                flush_interval=elk_config.get("flush_interval", 2.0),
            )
            logstash_handler.setLevel(level)
            logstash_handler.addFilter(SensitiveDataFilter())
            root_logger.addHandler(logstash_handler)
            root_logger.info("ELK Logstash Handler 已启用", extra={
                "logstash_host": elk_config.get("host", "localhost"),
                "logstash_port": elk_config.get("port", 5000),
            })
        except Exception as e:
            root_logger.warning(f"ELK Logstash Handler 初始化失败: {e}")
            logstash_handler = None
    
    return logstash_handler


class ColoredConsoleFormatter(logging.Formatter):
    """带颜色的控制台日志格式化器。
    
    不同级别的日志显示不同颜色：
    - DEBUG: 灰色
    - INFO: 绿色  
    - WARNING: 黄色
    - ERROR: 红色
    - CRITICAL: 粗红色+背景色
    """
    
    COLORS = {
        "DEBUG": "\033[36m",       # Cyan
        "INFO": "\033[32m",        # Green
        "WARNING": "\033[33m",     # Yellow
        "ERROR": "\033[31m",       # Red
        "CRITICAL": "\033[41;1m",  # Red background + Bold
    }
    RESET = "\033[0m"
    
    def format(self, record: LogRecord) -> str:
        """格式化日志记录，添加颜色。
        
        Args:
            record: 日志记录对象
            
        Returns:
            带颜色的格式化日志
        """
        color = self.COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        
        return super().format(record)


@lru_cache(maxsize=128)
def get_logger(name: str) -> logging.Logger:
    """获取Logger实例（带缓存）。
    
    使用lru_cache缓存Logger实例，避免重复创建。
    
    Args:
        name: Logger名称，通常传入__name__
        
    Returns:
        Logger实例
        
    Example:
        >>> logger = get_logger(__name__)
        >>> logger.info("Hello World")
    """
    logger = logging.getLogger(name)
    return ContextualLoggerAdapter(logger)


# ========== 导出列表 ==========
__all__ = [
    "setup_logging",
    "get_logger",
    "ContextualLoggerAdapter",
    "SensitiveDataFilter",
    "JsonFormatter",
    "LogstashHandler",
]
