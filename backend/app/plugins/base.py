"""插件系统 - 插件基类定义

定义所有插件的基类接口，包括：
- 基础插件 (BasePlugin)
- 文档加载器插件 (DocumentLoaderPlugin)
- LLM 提供商插件 (LLMProviderPlugin)
- 检索器插件 (RetrieverPlugin)
- 工具插件 (ToolPlugin)
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field


@dataclass
class PluginMetadata:
    """插件元信息

    Attributes:
        name: 插件唯一名称
        version: 插件版本号
        description: 插件描述
        author: 插件作者
        dependencies: 依赖的插件名称列表
    """

    name: str
    version: str
    description: str = ""
    author: str = ""
    dependencies: List[str] = field(default_factory=list)


class BasePlugin(ABC):
    """插件基类

    所有插件必须继承此类并实现以下方法：
    - metadata: 返回插件元信息
    - initialize: 插件初始化
    - shutdown: 插件关闭
    """

    @property
    @abstractmethod
    def metadata(self) -> PluginMetadata:
        """插件元信息"""
        ...

    @abstractmethod
    async def initialize(self) -> None:
        """插件初始化，在插件加载时调用"""
        ...

    @abstractmethod
    async def shutdown(self) -> None:
        """插件关闭，在插件卸载时调用"""
        ...

    async def health_check(self) -> bool:
        """健康检查（可选实现）

        Returns:
            True 表示插件正常运行
        """
        return True


class DocumentLoaderPlugin(BasePlugin):
    """文档加载器插件基类

    用于扩展系统支持的文档格式。
    每个加载器插件负责一种或多种文件格式的文档加载。
    """

    @abstractmethod
    async def supports_format(self, file_extension: str) -> bool:
        """检查是否支持此文件格式

        Args:
            file_extension: 文件扩展名（如 .pdf, .docx）

        Returns:
            True 表示支持此格式
        """
        ...

    @abstractmethod
    async def load(self, file_path: str, **kwargs) -> List[Any]:
        """加载文档并返回结构化内容

        Args:
            file_path: 文件路径
            **kwargs: 加载器特定参数

        Returns:
            结构化文档内容列表
        """
        ...


class LLMProviderPlugin(BasePlugin):
    """LLM 提供商插件基类

    用于集成不同的 LLM 服务提供商（如 OpenAI、阿里云、本地模型等）。
    """

    @abstractmethod
    async def chat(self, messages: List[Dict[str, str]], **kwargs) -> str:
        """同步聊天

        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            **kwargs: 模型参数（temperature, max_tokens 等）

        Returns:
            LLM 回复文本
        """
        ...

    @abstractmethod
    async def chat_stream(self, messages: List[Dict[str, str]], **kwargs):
        """流式聊天

        Args:
            messages: 消息列表
            **kwargs: 模型参数

        Yields:
            流式文本片段
        """
        ...

    @abstractmethod
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """文本嵌入

        Args:
            texts: 待嵌入的文本列表

        Returns:
            嵌入向量列表
        """
        ...


class RetrieverPlugin(BasePlugin):
    """检索器插件基类

    用于扩展不同的检索策略（向量检索、关键词检索、混合检索等）。
    """

    @abstractmethod
    async def search(
        self, query: str, top_k: int = 10, **kwargs
    ) -> List[Dict[str, Any]]:
        """执行检索

        Args:
            query: 查询文本
            top_k: 返回的最大结果数
            **kwargs: 检索器特定参数

        Returns:
            检索结果列表，每个结果包含 content, score, metadata 等字段
        """
        ...


class ToolPlugin(BasePlugin):
    """工具插件基类（供 Agent 调用）

    用于扩展 Agent 可调用的工具函数。
    工具定义遵循 OpenAI function calling 格式。
    """

    @abstractmethod
    def get_tool_definition(self) -> Dict[str, Any]:
        """返回 OpenAI function calling 格式的工具定义

        Returns:
            工具定义字典，格式如下：
            {
                "type": "function",
                "function": {
                    "name": "tool_name",
                    "description": "工具描述",
                    "parameters": {
                        "type": "object",
                        "properties": {...},
                        "required": [...]
                    }
                }
            }
        """
        ...

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """执行工具

        Args:
            **kwargs: 工具参数（由 LLM 根据 tool_definition 生成）

        Returns:
            工具执行结果
        """
        ...