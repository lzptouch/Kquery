"""Mock 图像理解数据。

当 DashScope 多模态 API 不可用时，返回预设的图像描述。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.mock_data import register_mock
from app.core.logger import get_logger

logger = get_logger(__name__)


def generate_mock_image_description(
    image_path: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """生成 Mock 图像描述。

    返回格式与 ImageUnderstandingResult 兼容。

    Args:
        image_path: 图像文件路径

    Returns:
        包含 description 等字段的字典
    """
    logger.info(f"生成 Mock 图像描述: image_path={image_path}")
    return {
        "image_path": image_path or "",
        "image_hash": "mock-hash",
        "description": (
            "这是 Mock 图像描述。由于多模态 LLM API 未配置或暂时不可用，"
            "当前返回预设的 Mock 数据。请配置阿里云 AccessKey Secret 以获取真实图像理解结果。"
        ),
        "description_level": kwargs.get("description_level", "standard"),
        "elements": [],
        "success": True,
        "error_message": None,
    }


# 注册到 Mock 注册表
register_mock("image_description", generate_mock_image_description)

__all__ = [
    "generate_mock_image_description",
]
