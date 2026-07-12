"""插件系统

提供插件化的架构，支持动态加载和扩展系统功能。
包括：
- base: 插件基类定义
- registry: 插件注册中心
- manager: 插件管理器
- builtin: 内置插件
"""

from app.plugins.base import (
    BasePlugin,
    PluginMetadata,
    DocumentLoaderPlugin,
    LLMProviderPlugin,
    RetrieverPlugin,
    ToolPlugin,
)
from app.plugins.registry import PluginRegistry, plugin_registry
from app.plugins.manager import PluginManager, plugin_manager

__all__ = [
    "BasePlugin",
    "PluginMetadata",
    "DocumentLoaderPlugin",
    "LLMProviderPlugin",
    "RetrieverPlugin",
    "ToolPlugin",
    "PluginRegistry",
    "plugin_registry",
    "PluginManager",
    "plugin_manager",
]