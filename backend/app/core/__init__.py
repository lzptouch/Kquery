"""KQuery Pro 核心模块。

提供全局共享的基础组件，包括：
- 异常类体系（exceptions）
- 配置管理（settings）
- 日志工具（logger）
- 安全工具（security）
- 中间件（middleware）
- 缓存预热（cache_warmer）

使用示例：
    from app.core.exceptions import KQueryError, ValidationError
    from app.core.settings import get_settings
    from app.core.logger import get_logger
    from app.core.cache_warmer import cache_warmer
    
    logger = get_logger(__name__)
    settings = get_settings()
"""

from __future__ import annotations

__all__ = ["exceptions", "settings", "logger", "cache_warmer"]
