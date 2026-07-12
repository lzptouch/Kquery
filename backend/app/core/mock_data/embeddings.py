"""Mock 嵌入向量数据。

当 LLM 嵌入 API 不可用时，返回固定维度的零向量或随机向量。
"""
from __future__ import annotations

import math
import random
from typing import List, Optional

from app.core.mock_data import register_mock
from app.core.logger import get_logger

logger = get_logger(__name__)

# 默认嵌入维度
_DEFAULT_DIM = 4096


def generate_mock_embeddings(
    texts: Optional[List[str]] = None,
    dimension: Optional[int] = None,
    count: int = 1,
) -> List[List[float]]:
    """生成 Mock 嵌入向量。

    Args:
        texts: 原始文本列表（用于确定返回数量）
        dimension: 向量维度（默认 4096）
        count: 向量数量（当 texts 为 None 时使用）

    Returns:
        固定维度的零向量列表
    """
    dim = dimension or _DEFAULT_DIM
    num = len(texts) if texts else count

    logger.info(
        f"生成 Mock 嵌入向量: {num} 个, 维度 {dim}"
    )
    return [[0.0] * dim for _ in range(num)]


def generate_mock_single_embedding(
    text: Optional[str] = None,
    dimension: Optional[int] = None,
) -> List[float]:
    """生成单个 Mock 嵌入向量。"""
    dim = dimension or _DEFAULT_DIM
    return [0.0] * dim


# 注册到 Mock 注册表
register_mock("embeddings", generate_mock_embeddings)
register_mock("single_embedding", generate_mock_single_embedding)

__all__ = [
    "generate_mock_embeddings",
    "generate_mock_single_embedding",
]
