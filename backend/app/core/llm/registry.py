"""LLM Provider 注册表。

从环境变量或配置初始化 Provider 列表，支持动态解析和默认 Provider。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

from app.core.llm.models import LLMProvider, ProviderCapability, ProviderType
from app.core.logger import get_logger

logger = get_logger(__name__)


class LLMProviderRegistry:
    """LLM Provider 注册表。

    管理所有可用的 LLM Provider，支持按名称解析、列表查询和默认 Provider。

    使用方式：
        registry = LLMProviderRegistry()
        registry.register(LLMProvider(...))
        provider = registry.resolve("deepseek-chat")
        models = registry.list_models()
    """

    def __init__(self):
        self._providers: Dict[str, LLMProvider] = {}

    def register(self, provider: LLMProvider) -> None:
        """注册一个 Provider。"""
        self._providers[provider.name] = provider
        logger.info(f"已注册 LLM Provider: {provider.name} ({provider.display_name})")

    def unregister(self, name: str) -> None:
        """注销一个 Provider。"""
        self._providers.pop(name, None)

    def resolve(self, name: Optional[str] = None) -> Optional[LLMProvider]:
        """按名称解析 Provider。

        Args:
            name: Provider 名称，为 None 时返回默认 Provider

        Returns:
            LLMProvider 或 None
        """
        if name:
            return self._providers.get(name)
        return self.get_default()

    def get_default(self) -> Optional[LLMProvider]:
        """获取默认 Provider。"""
        for p in self._providers.values():
            if p.is_default:
                return p
        # fallback: 返回第一个注册的
        if self._providers:
            return next(iter(self._providers.values()))
        return None

    def list_models(self) -> List[dict]:
        """列出所有可用模型（前端友好格式）。"""
        return [p.to_dict() for p in self._providers.values()]

    def get_all(self) -> List[LLMProvider]:
        """获取所有 Provider 原始对象。"""
        return list(self._providers.values())

    def has(self, name: str) -> bool:
        """检查 Provider 是否存在。"""
        return name in self._providers

    def count(self) -> int:
        """获取已注册 Provider 数量。"""
        return len(self._providers)

    def is_empty(self) -> bool:
        """检查是否为空。"""
        return len(self._providers) == 0

    @classmethod
    def from_settings(cls, settings) -> "LLMProviderRegistry":
        """从配置初始化 Provider 注册表。

        优先从 LLM_PROVIDERS 环境变量（JSON）解析，
        否则基于现有 settings.llm.agent_* 创建默认 Provider。
        """
        registry = cls()

        # 尝试从 JSON 环境变量解析
        providers_json = os.getenv("LLM_PROVIDERS", "")
        if providers_json:
            try:
                providers_data = json.loads(providers_json)
                for pdata in providers_data:
                    capabilities_raw = pdata.get("capabilities", ["chat", "stream"])
                    capabilities = [
                        ProviderCapability(c) if isinstance(c, str) else c
                        for c in capabilities_raw
                    ]
                    provider = LLMProvider(
                        name=pdata.get("name", pdata.get("model", "")),
                        display_name=pdata.get("display", pdata.get("display_name", pdata.get("name", ""))),
                        provider_type=pdata.get("provider", "openai"),
                        model=pdata.get("model", ""),
                        base_url=pdata.get("base_url", ""),
                        api_key=pdata.get("api_key"),
                        capabilities=capabilities,
                        is_default=pdata.get("is_default", False),
                        temperature=pdata.get("temperature", 0.1),
                        max_tokens=pdata.get("max_tokens", 2048),
                        description=pdata.get("description", ""),
                    )
                    registry.register(provider)

                if registry.count() > 0:
                    logger.info(f"从 LLM_PROVIDERS 环境变量加载了 {registry.count()} 个 Provider")
                    return registry
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"解析 LLM_PROVIDERS 环境变量失败: {e}，回退到默认配置")

        # 从现有 settings 创建默认 Provider
        agent_model = settings.llm.agent_model_name
        agent_base = settings.llm.agent_api_base
        agent_key = settings.llm.agent_api_key

        if agent_base and agent_model:
            # 默认 DashScope Provider
            provider = LLMProvider(
                name=agent_model,
                display_name=f"通义千问 ({agent_model})",
                provider_type="openai",
                model=agent_model,
                base_url=agent_base,
                api_key=agent_key,
                capabilities=[ProviderCapability.CHAT, ProviderCapability.STREAM, ProviderCapability.MULTIMODAL],
                is_default=True,
                description="默认 DashScope 模型",
            )
            registry.register(provider)

            # 同时注册多模态模型
            multimodal_model = settings.llm.multimodal_model_name
            if multimodal_model and multimodal_model != agent_model:
                multimodal_provider = LLMProvider(
                    name=multimodal_model,
                    display_name=f"多模态 ({multimodal_model})",
                    provider_type=settings.llm.multimodal_provider,
                    model=multimodal_model,
                    base_url=settings.llm.multimodal_api_base or agent_base,
                    api_key=settings.llm.multimodal_api_key or agent_key,
                    capabilities=[ProviderCapability.CHAT, ProviderCapability.MULTIMODAL],
                    description="多模态视觉模型",
                )
                registry.register(multimodal_provider)

            logger.info(f"从现有 Settings 初始化了 {registry.count()} 个默认 Provider")

        return registry


# 全局单例
_registry: Optional[LLMProviderRegistry] = None


def get_llm_registry() -> LLMProviderRegistry:
    """获取全局 LLM Provider 注册表单例。"""
    global _registry
    if _registry is None:
        from app.core.settings import get_settings

        settings = get_settings()
        _registry = LLMProviderRegistry.from_settings(settings)
        if _registry.is_empty():
            logger.warning("LLM Provider 注册表为空，LLM 功能可能不可用")
    return _registry


def reset_llm_registry() -> None:
    """重置全局注册表（用于测试或热更新）。"""
    global _registry
    _registry = None
