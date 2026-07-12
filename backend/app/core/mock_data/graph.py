"""Mock 知识图谱抽取数据。

当 LLM 图谱抽取 API 不可用时，返回预设的 Mock 实体和关系。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.mock_data import register_mock
from app.core.logger import get_logger

logger = get_logger(__name__)


def generate_mock_graph_extraction(
    text: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """生成 Mock 图谱抽取结果。

    当 LLM API 未配置时返回空结果，避免占位实体误导用户。
    真正的错误信息由上层服务统一返回。

    Returns:
        包含 entities 和 relations 的字典
    """
    logger.warning("LLM API 未配置，Mock 图谱抽取返回空结果")
    return {
        "entities": [],
        "relations": [],
        "processing_time_ms": 0.0,
        "raw_response": '{"entities": [], "relations": []}',
    }


def generate_mock_entity_extraction(
    text: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """生成 Mock 实体抽取结果。

    当 LLM API 未配置时返回空结果，避免占位实体误导用户。
    真正的错误信息由上层服务统一返回。

    Returns:
        包含 entities 列表的字典
    """
    logger.warning("LLM API 未配置，Mock 实体抽取返回空结果")
    return {
        "entities": [],
        "text_length": len(text) if text else 0,
        "processing_time_ms": 0.0,
        "llm_count": 0,
        "rule_count": 0,
        "merged_count": 0,
        "errors": [],
    }


# 注册到 Mock 注册表
register_mock("graph_extraction", generate_mock_graph_extraction)
register_mock("entity_extraction", generate_mock_entity_extraction)

__all__ = [
    "generate_mock_graph_extraction",
    "generate_mock_entity_extraction",
]
