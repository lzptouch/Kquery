"""Mock HyDE 假设性文档数据。

当 LLM HyDE 生成 API 不可用时，返回原始查询作为假设性文档。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.core.mock_data import register_mock
from app.core.logger import get_logger

logger = get_logger(__name__)


def generate_mock_hyde_document(
    query: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """生成 Mock HyDE 假设性文档。

    返回格式与 HyDEGenerationResult 兼容。

    Args:
        query: 原始查询

    Returns:
        包含 hypothetical_doc 等字段的字典
    """
    logger.info(f"生成 Mock HyDE 文档: query={query}")
    return {
        "hypothetical_doc": query or "",
        "original_query": query or "",
        "generation_time_ms": 0.0,
        "success": True,
        "error_message": None,
    }


# 注册到 Mock 注册表
register_mock("hyde_document", generate_mock_hyde_document)

__all__ = [
    "generate_mock_hyde_document",
]
