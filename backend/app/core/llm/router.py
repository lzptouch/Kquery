"""LLM Provider 统一路由解析。

提供按模型名解析 Provider 配置的便捷函数。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from app.core.llm.models import LLMProvider
from app.core.llm.registry import get_llm_registry
from app.core.logger import get_logger

logger = get_logger(__name__)


def resolve_provider(model: Optional[str] = None) -> Optional[LLMProvider]:
    """按模型名解析 Provider。

    Args:
        model: 模型名称，为 None 时返回默认 Provider

    Returns:
        LLMProvider 或 None
    """
    registry = get_llm_registry()
    return registry.resolve(model)


def resolve_provider_config(model: Optional[str] = None) -> Dict[str, Any]:
    """解析 Provider 配置为 llm_client 可用格式。

    Args:
        model: 模型名称

    Returns:
        包含 api_key, base_url, provider_type, model 的字典
    """
    provider = resolve_provider(model)
    if provider is None:
        # 回退到默认配置
        from app.core.settings import get_settings

        settings = get_settings()
        return {
            "api_key": settings.llm.agent_api_key,
            "base_url": settings.llm.agent_api_base,
            "provider_type": "openai",
            "model": settings.llm.agent_model_name,
        }

    return {
        "api_key": provider.api_key,
        "base_url": provider.base_url,
        "provider_type": provider.provider_type,
        "model": provider.model,
    }
