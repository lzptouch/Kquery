"""LLM Provider 数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class ProviderType(str, Enum):
    """Provider 类型枚举。"""
    OPENAI = "openai"
    DASHSCOPE = "dashscope"
    LANGCHAIN = "langchain"


class ProviderCapability(str, Enum):
    """Provider 能力枚举。"""
    CHAT = "chat"
    STREAM = "stream"
    EMBEDDING = "embedding"
    MULTIMODAL = "multimodal"
    RERANK = "rerank"


@dataclass
class LLMProvider:
    """LLM Provider 配置数据类。

    Attributes:
        name: 唯一标识名（如 qwen-turbo, deepseek-chat）
        display_name: 显示名称（如 通义千问 Turbo）
        provider_type: 接入类型
        model: 模型名称
        base_url: API 基础 URL
        api_key: API 密钥
        capabilities: 支持的能力列表
        is_default: 是否为默认模型
        temperature: 默认温度参数
        max_tokens: 默认最大 token 数
        description: 模型描述
    """
    name: str
    display_name: str
    provider_type: str = "openai"
    model: str = ""
    base_url: str = ""
    api_key: Optional[str] = None
    capabilities: List[ProviderCapability] = field(default_factory=lambda: [ProviderCapability.CHAT, ProviderCapability.STREAM])
    is_default: bool = False
    temperature: float = 0.1
    max_tokens: int = 2048
    description: str = ""

    def __post_init__(self):
        if not self.model:
            self.model = self.name

    def to_dict(self) -> dict:
        """转为前端友好的字典（隐藏 api_key）。"""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "provider_type": self.provider_type,
            "model": self.model,
            "capabilities": [c.value for c in self.capabilities],
            "is_default": self.is_default,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "description": self.description,
        }

    def has_capability(self, capability: ProviderCapability) -> bool:
        """检查是否支持某项能力。"""
        return capability in self.capabilities
