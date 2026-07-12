"""Mock 问答数据。

当 LLM 问答 API 不可用时，返回预设的 Mock 答案。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.core.mock_data import register_mock
from app.core.logger import get_logger

logger = get_logger(__name__)


def generate_mock_qa_answer(
    question: Optional[str] = None,
    **kwargs,
) -> str:
    """生成 Mock 问答答案。

    Args:
        question: 用户问题

    Returns:
        Mock 答案文本
    """
    logger.info(f"生成 Mock 问答答案: question={question}")
    return (
        f"这是关于「{question or '您的问题'}」的 Mock 回答。"
        "由于 LLM API 未配置或暂时不可用，当前返回预设的 Mock 数据。"
        "请配置正确的 LLM API 密钥以获取真实回答。"
    )


def generate_mock_chat_response(
    user_input: Optional[str] = None,
    **kwargs,
) -> Dict[str, Any]:
    """生成 Mock 聊天响应。

    返回格式与 qa_agent 的 chat/chat_general 方法兼容。

    Returns:
        包含 response 等字段的字典
    """
    logger.info(f"生成 Mock 聊天响应: user_input={user_input}")
    return {
        "success": True,
        "response": (
            f"这是对「{user_input or '您的输入'}」的 Mock 回复。"
            "由于 LLM API 未配置，当前使用 Mock 数据模式。"
        ),
        "session_id": kwargs.get("session_id", "mock-session"),
        "mode": "mock",
    }


def generate_mock_multimodal_chat_response(
    user_input: Optional[str] = None,
    image_count: int = 1,
    **kwargs,
) -> str:
    """生成 Mock 多模态聊天响应。

    当多模态模型未配置时返回，用于保持接口可用性。

    Returns:
        Mock 响应文本
    """
    logger.info(f"生成 Mock 多模态聊天响应: user_input={user_input}, image_count={image_count}")
    return (
        f"已收到您的图片（共 {image_count} 张）。"
        "当前多模态模型未配置 API Key，这是 Mock 响应。"
        f"您的问题是：{user_input or ''}"
    )



# 注册到 Mock 注册表
register_mock("qa_answer", generate_mock_qa_answer)
register_mock("chat_response", generate_mock_chat_response)
register_mock("multimodal_chat_response", generate_mock_multimodal_chat_response)

__all__ = [
    "generate_mock_qa_answer",
    "generate_mock_chat_response",
    "generate_mock_multimodal_chat_response",
]

