"""插件系统 - 插件管理器

负责插件的自动发现、加载、初始化和生命周期管理。
支持从指定目录自动扫描并加载插件。
"""

import importlib
import os
from pathlib import Path
from typing import List, Optional, Type

from app.plugins.base import BasePlugin
from app.plugins.registry import plugin_registry
from app.core.logger import get_logger

logger = get_logger(__name__)


class PluginManager:
    """插件管理器

    负责管理插件的完整生命周期：
    - 自动发现：扫描指定目录中的插件模块
    - 自动加载：动态导入插件模块
    - 自动注册：将插件实例注册到 PluginRegistry
    - 初始化/关闭：管理插件的启动和停止

    使用示例:
        manager = PluginManager()

        # 自动发现并加载插件
        await manager.discover_and_load("app/plugins/builtin")

        # 初始化所有插件
        await manager.initialize_all()

        # 关闭所有插件
        await manager.shutdown_all()
    """

    def __init__(self):
        self._loaded_plugins: List[BasePlugin] = []

    async def discover_and_load(
        self, plugin_dir: str, recursive: bool = True
    ) -> int:
        """自动发现并加载指定目录中的插件

        扫描目录下所有 Python 模块，查找并实例化 BasePlugin 的子类。

        Args:
            plugin_dir: 插件目录路径（相对于项目根目录或绝对路径）
            recursive: 是否递归扫描子目录

        Returns:
            成功加载的插件数量
        """
        plugin_path = Path(plugin_dir)
        if not plugin_path.is_absolute():
            # 尝试相对于 backend 目录
            backend_dir = Path(__file__).parent.parent.parent
            plugin_path = backend_dir / plugin_dir

        if not plugin_path.exists():
            logger.warning(f"插件目录不存在: {plugin_path}")
            return 0

        count = 0
        pattern = "**/*.py" if recursive else "*.py"

        for py_file in plugin_path.glob(pattern):
            if py_file.name.startswith("_") or py_file.name == "base.py":
                continue

            try:
                module_path = self._get_module_path(py_file)
                module = importlib.import_module(module_path)

                # 查找模块中的插件类
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, BasePlugin)
                        and attr is not BasePlugin
                        and not attr.__name__.startswith("_")
                    ):
                        plugin = attr()
                        plugin_registry.register(plugin)
                        self._loaded_plugins.append(plugin)
                        count += 1
                        logger.info(
                            f"发现并加载插件: {attr.__name__} "
                            f"({plugin.metadata.name} v{plugin.metadata.version})"
                        )

            except Exception as e:
                logger.error(f"加载插件模块失败: {py_file}, 错误: {e}")

        logger.info(f"插件发现完成，共加载 {count} 个插件")
        return count

    def _get_module_path(self, file_path: Path) -> str:
        """将文件路径转换为 Python 模块路径"""
        backend_dir = Path(__file__).parent.parent.parent
        try:
            relative = file_path.relative_to(backend_dir)
        except ValueError:
            # 如果不是在 backend 下，尝试直接使用
            return str(file_path.with_suffix("")).replace(os.sep, ".")

        parts = list(relative.parts)
        parts[-1] = Path(parts[-1]).stem
        return ".".join(parts)

    async def initialize_all(self) -> None:
        """初始化所有已加载的插件"""
        await plugin_registry.initialize_all()
        logger.info(f"所有插件初始化完成，共 {len(self._loaded_plugins)} 个")

    async def shutdown_all(self) -> None:
        """关闭所有已加载的插件"""
        await plugin_registry.shutdown_all()
        self._loaded_plugins.clear()
        logger.info("所有插件已关闭")

    def get_loaded_plugins(self) -> List[BasePlugin]:
        """获取所有已加载的插件"""
        return self._loaded_plugins

    def get_stats(self) -> dict:
        """获取插件管理器统计信息"""
        return {
            "loaded_plugins": len(self._loaded_plugins),
            **plugin_registry.get_stats(),
        }


# 全局插件管理器
plugin_manager = PluginManager()