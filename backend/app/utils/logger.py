"""
Kquery日志配置（兼容层）
委托给 app.core.logger 实现。
"""

from app.core.logger import get_logger, setup_logging  # noqa: F401

__all__ = ["get_logger", "setup_logging"]
