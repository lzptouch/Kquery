"""插件系统 - 插件注册中心

管理所有插件的注册、查找和生命周期。
采用单例模式，确保全局唯一的插件注册中心。
"""

from typing import Dict, List, Optional, Type, Any

from app.plugins.base import (
    BasePlugin,
    PluginMetadata,
    DocumentLoaderPlugin,
    LLMProviderPlugin,
    RetrieverPlugin,
    ToolPlugin,
)
from app.core.logger import get_logger

logger = get_logger(__name__)


class PluginRegistry:
    """插件注册中心（单例模式）

    管理所有插件的注册、查找、初始化和关闭。

    使用示例:
        registry = PluginRegistry()

        # 注册插件
        registry.register_document_loader(PDFLoaderPlugin())

        # 根据格式获取加载器
        loader = registry.get_loader_for_format(".pdf")

        # 初始化所有插件
        await registry.initialize_all()
    """

    _instance: Optional["PluginRegistry"] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._document_loaders: Dict[str, DocumentLoaderPlugin] = {}
        self._llm_providers: Dict[str, LLMProviderPlugin] = {}
        self._retrievers: Dict[str, RetrieverPlugin] = {}
        self._tools: Dict[str, ToolPlugin] = {}
        self._all_plugins: Dict[str, BasePlugin] = {}

        logger.info("PluginRegistry 初始化完成")

    # ========== 注册方法 ==========

    def register_document_loader(self, plugin: DocumentLoaderPlugin) -> None:
        """注册文档加载器插件"""
        self._document_loaders[plugin.metadata.name] = plugin
        self._all_plugins[plugin.metadata.name] = plugin
        logger.info(
            f"注册文档加载器插件: {plugin.metadata.name} v{plugin.metadata.version}"
        )

    def register_llm_provider(self, plugin: LLMProviderPlugin) -> None:
        """注册 LLM 提供商插件"""
        self._llm_providers[plugin.metadata.name] = plugin
        self._all_plugins[plugin.metadata.name] = plugin
        logger.info(
            f"注册 LLM 提供商插件: {plugin.metadata.name} v{plugin.metadata.version}"
        )

    def register_retriever(self, plugin: RetrieverPlugin) -> None:
        """注册检索器插件"""
        self._retrievers[plugin.metadata.name] = plugin
        self._all_plugins[plugin.metadata.name] = plugin
        logger.info(
            f"注册检索器插件: {plugin.metadata.name} v{plugin.metadata.version}"
        )

    def register_tool(self, plugin: ToolPlugin) -> None:
        """注册工具插件"""
        self._tools[plugin.metadata.name] = plugin
        self._all_plugins[plugin.metadata.name] = plugin
        logger.info(
            f"注册工具插件: {plugin.metadata.name} v{plugin.metadata.version}"
        )

    def register(self, plugin: BasePlugin) -> None:
        """通用注册方法（自动判断插件类型）"""
        if isinstance(plugin, DocumentLoaderPlugin):
            self.register_document_loader(plugin)
        elif isinstance(plugin, LLMProviderPlugin):
            self.register_llm_provider(plugin)
        elif isinstance(plugin, RetrieverPlugin):
            self.register_retriever(plugin)
        elif isinstance(plugin, ToolPlugin):
            self.register_tool(plugin)
        else:
            logger.warning(f"未知插件类型: {type(plugin).__name__}")

    # ========== 查找方法 ==========

    def get_loader_for_format(
        self, file_extension: str
    ) -> Optional[DocumentLoaderPlugin]:
        """根据文件扩展名获取对应的文档加载器

        Args:
            file_extension: 文件扩展名（如 .pdf, .docx）

        Returns:
            匹配的文档加载器插件，或 None
        """
        for loader in self._document_loaders.values():
            if loader.supports_format(file_extension):
                return loader
        return None

    def get_llm_provider(self, name: str) -> Optional[LLMProviderPlugin]:
        """根据名称获取 LLM 提供商

        Args:
            name: 提供商名称

        Returns:
            LLM 提供商插件，或 None
        """
        return self._llm_providers.get(name)

    def get_retriever(self, name: str) -> Optional[RetrieverPlugin]:
        """根据名称获取检索器

        Args:
            name: 检索器名称

        Returns:
            检索器插件，或 None
        """
        return self._retrievers.get(name)

    def get_tool(self, name: str) -> Optional[ToolPlugin]:
        """根据名称获取工具

        Args:
            name: 工具名称

        Returns:
            工具插件，或 None
        """
        return self._tools.get(name)

    def get_plugin(self, name: str) -> Optional[BasePlugin]:
        """根据名称获取任意类型插件

        Args:
            name: 插件名称

        Returns:
            插件实例，或 None
        """
        return self._all_plugins.get(name)

    # ========== 列表方法 ==========

    def list_document_loaders(self) -> List[PluginMetadata]:
        """列出所有文档加载器插件"""
        return [p.metadata for p in self._document_loaders.values()]

    def list_llm_providers(self) -> List[PluginMetadata]:
        """列出所有 LLM 提供商插件"""
        return [p.metadata for p in self._llm_providers.values()]

    def list_retrievers(self) -> List[PluginMetadata]:
        """列出所有检索器插件"""
        return [p.metadata for p in self._retrievers.values()]

    def list_tools(self) -> List[Dict[str, Any]]:
        """列出所有工具的定义（OpenAI function calling 格式）"""
        return [p.get_tool_definition() for p in self._tools.values()]

    def list_all_plugins(self) -> List[PluginMetadata]:
        """列出所有已注册的插件"""
        return [p.metadata for p in self._all_plugins.values()]

    # ========== 生命周期管理 ==========

    async def initialize_all(self) -> None:
        """初始化所有已注册的插件"""
        for name, plugin in self._all_plugins.items():
            try:
                await plugin.initialize()
                logger.info(f"插件初始化成功: {name}")
            except Exception as e:
                logger.error(f"插件初始化失败: {name}, 错误: {e}")

    async def shutdown_all(self) -> None:
        """关闭所有已注册的插件"""
        for name, plugin in self._all_plugins.items():
            try:
                await plugin.shutdown()
                logger.info(f"插件关闭成功: {name}")
            except Exception as e:
                logger.error(f"插件关闭失败: {name}, 错误: {e}")

    async def health_check_all(self) -> Dict[str, bool]:
        """对所有插件执行健康检查

        Returns:
            插件名称到健康状态的映射
        """
        results = {}
        for name, plugin in self._all_plugins.items():
            try:
                results[name] = await plugin.health_check()
            except Exception as e:
                logger.error(f"插件健康检查失败: {name}, 错误: {e}")
                results[name] = False
        return results

    # ========== 卸载方法 ==========

    def unregister(self, name: str) -> bool:
        """卸载指定插件

        Args:
            name: 插件名称

        Returns:
            True 表示卸载成功
        """
        plugin = self._all_plugins.pop(name, None)
        if plugin is None:
            return False

        self._document_loaders.pop(name, None)
        self._llm_providers.pop(name, None)
        self._retrievers.pop(name, None)
        self._tools.pop(name, None)

        logger.info(f"插件已卸载: {name}")
        return True

    def get_stats(self) -> Dict[str, int]:
        """获取插件统计信息"""
        return {
            "total_plugins": len(self._all_plugins),
            "document_loaders": len(self._document_loaders),
            "llm_providers": len(self._llm_providers),
            "retrievers": len(self._retrievers),
            "tools": len(self._tools),
        }


# 全局插件注册中心
plugin_registry = PluginRegistry()