"""Mock 数据注册表。

所有 Mock 数据模块在此注册，统一通过 get_mock_data 工厂函数获取。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.logger import get_logger

logger = get_logger(__name__)

# Mock 数据注册表：module_name -> callable(**kwargs) -> mock_data
MOCK_REGISTRY: Dict[str, Any] = {}


def register_mock(module_name: str, generator_fn: Any) -> None:
    """注册 Mock 数据生成器。"""
    MOCK_REGISTRY[module_name] = generator_fn
    logger.debug(f"Mock 数据模块已注册: {module_name}")


def get_mock_data(module_name: str, **kwargs) -> Any:
    """获取指定模块的 Mock 数据。

    Args:
        module_name: 模块名称（如 "embeddings", "graph", "qa", "hyde", "image"）
        **kwargs: 传递给 Mock 生成器的参数

    Returns:
        Mock 数据，类型与真实 LLM 返回值一致

    Raises:
        ValueError: 模块未注册
    """
    if module_name not in MOCK_REGISTRY:
        raise ValueError(
            f"Mock 数据模块 '{module_name}' 未注册。"
            f"可用模块: {list(MOCK_REGISTRY.keys())}"
        )
    logger.info(f"使用 Mock 数据模块: {module_name}")
    return MOCK_REGISTRY[module_name](**kwargs)


# 导入子模块触发注册
from app.core.mock_data import embeddings  # noqa: F401, E402
from app.core.mock_data import graph  # noqa: F401, E402
from app.core.mock_data import qa  # noqa: F401, E402
from app.core.mock_data import hyde  # noqa: F401, E402
from app.core.mock_data import image  # noqa: F401, E402

__all__ = [
    "register_mock",
    "get_mock_data",
    "MOCK_REGISTRY",
]
