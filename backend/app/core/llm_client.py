"""统一 LLM 调用客户端。

封装所有 LLM 调用方式（OpenAI 兼容接口 / DashScope / LangChain ChatOpenAI），
提供统一的调用入口，包含：
- tenacity 指数退避重试
- trace_span 链路追踪
- Mock 数据降级（API 未配置或重试失败时）
- API Key 统一检测

使用示例：
    from app.core.llm_client import call_openai_chat, call_dashscope, is_llm_configured

    # 检查是否配置
    if not is_llm_configured("agent"):
        result = get_mock_data("qa_answer", question=query)
        return result

    # 调用 LLM（自动重试 + 追踪）
    response = await call_openai_chat(
        model="qwen-turbo",
        messages=[{"role": "user", "content": "你好"}],
        api_key=settings.llm.agent_api_key,
        base_url=settings.llm.agent_api_base,
    )
"""
from __future__ import annotations

import asyncio
import base64
import logging
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union


from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.core.exceptions import ExternalServiceError, ServiceTimeoutError
from app.core.logger import get_logger
from app.core.settings import get_settings
from app.core.tracing import get_tracer

logger = get_logger(__name__)

T = TypeVar("T")

# ─── 重试条件 ────────────────────────────────────────────────


def _is_retryable_exception(exc: BaseException) -> bool:
    """判断异常是否可重试。

    认证错误（401/403）和模型不存在错误不重试，直接降级到 Mock。
    """
    exc_str = str(exc).lower()

    # 不重试：认证错误
    if "401" in exc_str or "unauthorized" in exc_str:
        return False
    if "403" in exc_str or "forbidden" in exc_str:
        return False
    if "api_key" in exc_str and ("invalid" in exc_str or "not found" in exc_str):
        return False

    # 不重试：模型不存在
    if "model" in exc_str and "not found" in exc_str:
        return False
    if "model" in exc_str and "not exist" in exc_str:
        return False

    # 可重试：超时、连接错误、限流、服务不可用
    return True


def _is_timeout_exception(exc: BaseException) -> bool:
    """判断是否为超时异常。"""
    exc_str = str(exc).lower()
    return any(
        keyword in exc_str
        for keyword in ["timeout", "timed out", "deadline exceeded"]
    )


# ─── 重试装饰器 ──────────────────────────────────────────────


def with_llm_retry(
    mock_module: Optional[str] = None,
    max_attempts: int = 3,
    min_wait: float = 1.0,
    max_wait: float = 10.0,
) -> Callable:
    """LLM 调用重试装饰器。

    在 tenacity 重试全部失败后，自动降级到 Mock 数据。

    Args:
        mock_module: Mock 数据模块名称（如 "qa_answer"），为 None 时不降级
        max_attempts: 最大重试次数
        min_wait: 最小等待时间（秒）
        max_wait: 最大等待时间（秒）
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # 尝试带重试的 LLM 调用
            retry_decorator = retry(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
                before_sleep=before_sleep_log(logger, logging.WARNING),
                retry=retry_if_exception(_is_retryable_exception),
                reraise=True,
            )

            try:
                return await retry_decorator(func)(*args, **kwargs)
            except Exception as e:
                logger.warning(
                    f"LLM 调用重试 {max_attempts} 次后仍失败: {e!r}"
                )
                # 降级到 Mock 数据
                if mock_module:
                    from app.core.mock_data import get_mock_data

                    logger.info(f"降级到 Mock 数据: {mock_module}")
                    return get_mock_data(mock_module, **kwargs)
                raise

        return wrapper

    return decorator


# ─── API Key 检测 ────────────────────────────────────────────


def is_llm_configured(service_type: str = "agent") -> bool:
    """检测指定类型的 LLM 服务是否已配置 API Key。

    Args:
        service_type: 服务类型
            - "agent": 对话/Agent LLM（settings.llm.agent_api_key）
            - "embedding": 嵌入模型（settings.llm.embedding_api_key）
            - "reranker": 重排序模型（settings.llm.rerank_api_key）
            - "aliyun_vision": 阿里云视觉（settings.aliyun.access_key_secret）
            - "dashscope": DashScope 通用（DASHSCOPE_API_KEY 环境变量）
            - "openai": OpenAI 通用（OPENAI_API_KEY 环境变量）

    Returns:
        True 如果已配置
    """
    import os

    settings = get_settings()

    key_sources = {
        "agent": lambda: settings.llm.agent_api_key,
        "embedding": lambda: settings.llm.embedding_api_key,
        "reranker": lambda: settings.llm.rerank_api_key,
        "aliyun_vision": lambda: settings.aliyun.access_key_secret,
        "dashscope": lambda: os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("LLM_AGENT_API_KEY")
        or settings.llm.agent_api_key,
        "openai": lambda: os.getenv("OPENAI_API_KEY")
        or os.getenv("LLM_AGENT_API_KEY")
        or settings.llm.agent_api_key,
    }

    getter = key_sources.get(service_type)
    if getter is None:
        logger.warning(f"未知的 LLM 服务类型: {service_type}")
        return False

    key = getter()
    return bool(key and str(key).strip() not in {"", "None"})


# ─── 链路追踪辅助 ────────────────────────────────────────────


def _trace_llm_call(
    span_name: str,
    model: str,
    provider: str,
) -> Any:
    """创建 LLM 调用的追踪 span。"""
    tracer = get_tracer()
    if tracer is None:
        return _NullContextManager()

    span = tracer.start_as_current_span(span_name)
    span.set_attribute("llm.model", model)
    span.set_attribute("llm.provider", provider)
    return span


class _NullContextManager:
    """当追踪未配置时的空上下文管理器。"""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ─── OpenAI 兼容接口调用 ─────────────────────────────────────


async def call_openai_chat(
    model: str,
    messages: List[Dict[str, str]],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.1,
    timeout: Optional[float] = None,
    mock_module: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """调用 OpenAI 兼容的聊天接口（AsyncOpenAI）。

    自动处理重试、追踪和 Mock 降级。

    Args:
        model: 模型名称
        messages: 消息列表
        api_key: API 密钥
        base_url: API 基础 URL
        max_tokens: 最大生成 token 数
        temperature: 温度参数
        timeout: 超时时间（秒）
        mock_module: Mock 数据模块名称（如 "qa_answer"）
        **kwargs: 其他参数

    Returns:
        响应文本内容
    """
    from openai import AsyncOpenAI

    settings = get_settings()
    safe_key = api_key or settings.llm.agent_api_key or "sk-placeholder"
    safe_base = base_url or settings.llm.agent_api_base
    safe_timeout = timeout or settings.llm.embedding_timeout

    client = AsyncOpenAI(
        api_key=safe_key,
        base_url=safe_base,
        timeout=safe_timeout,
    )

    @with_llm_retry(mock_module=mock_module)
    async def _do_call() -> str:
        with _trace_llm_call("llm_openai_chat", model, "openai"):
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                **kwargs,
            )
            return response.choices[0].message.content or ""

    return await _do_call()


async def call_openai_embedding(
    texts: List[str],
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
    mock_module: Optional[str] = None,
) -> List[List[float]]:
    """调用 OpenAI 兼容的嵌入接口。

    Args:
        texts: 文本列表
        model: 模型名称
        api_key: API 密钥
        base_url: API 基础 URL
        timeout: 超时时间
        mock_module: Mock 数据模块名称（如 "embeddings"）

    Returns:
        嵌入向量列表
    """
    from openai import AsyncOpenAI

    settings = get_settings()
    safe_key = api_key or settings.llm.embedding_api_key or "sk-placeholder"
    safe_base = base_url or settings.llm.embedding_api_base
    safe_timeout = timeout or settings.llm.embedding_timeout
    safe_model = model or settings.llm.embedding_model_name

    client = AsyncOpenAI(
        api_key=safe_key,
        base_url=safe_base,
        timeout=safe_timeout,
    )

    @with_llm_retry(mock_module=mock_module)
    async def _do_call() -> List[List[float]]:
        with _trace_llm_call("llm_openai_embedding", safe_model, "openai"):
            resp = await client.embeddings.create(
                model=safe_model,
                input=texts,
            )
            return [item.embedding for item in resp.data]

    return await _do_call()


# ─── DashScope 调用 ──────────────────────────────────────────


async def call_dashscope(
    model: str,
    messages: List[Dict[str, str]],
    api_key: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.1,
    mock_module: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """调用 DashScope Generation API。

    Args:
        model: 模型名称
        messages: 消息列表
        api_key: API 密钥
        max_tokens: 最大生成 token 数
        temperature: 温度参数
        mock_module: Mock 数据模块名称
        **kwargs: 其他参数

    Returns:
        响应文本内容
    """
    import os

    import dashscope

    settings = get_settings()
    safe_key = (
        api_key
        or os.getenv("DASHSCOPE_API_KEY")
        or os.getenv("LLM_AGENT_API_KEY")
        or settings.llm.agent_api_key
    )
    dashscope.api_key = safe_key

    @with_llm_retry(mock_module=mock_module)
    async def _do_call() -> str:
        with _trace_llm_call("llm_dashscope", model, "dashscope"):
            response = dashscope.Generation.call(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                result_format="message",
                **kwargs,
            )
            if response.status_code != 200:
                raise ExternalServiceError(
                    f"DashScope API 错误: {response.message}",
                    service_name="dashscope",
                )
            return response.output.choices[0].message.content

    return await _do_call()


async def call_dashscope_multimodal(
    model: str,
    messages: List[Dict[str, Any]],
    api_key: Optional[str] = None,
    mock_module: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """调用 DashScope 多模态 API（图像理解等）。

    Args:
        model: 模型名称
        messages: 消息列表（包含图像数据）
        api_key: API 密钥
        mock_module: Mock 数据模块名称
        **kwargs: 其他参数

    Returns:
        响应文本内容
    """
    import os

    import dashscope

    safe_key = api_key or os.getenv("DASHSCOPE_API_KEY")
    dashscope.api_key = safe_key

    @with_llm_retry(mock_module=mock_module)
    async def _do_call() -> str:
        with _trace_llm_call("llm_dashscope_multimodal", model, "dashscope"):
            response = dashscope.MultiModalConversation.call(
                model=model,
                messages=messages,
                **kwargs,
            )
            if response.status_code == 200:
                content = response.output.choices[0].message.content
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and "text" in item:
                            return item["text"]
                return str(content)
            else:
                raise ExternalServiceError(
                    f"DashScope 多模态 API 错误: {response.message}",
                    service_name="dashscope_multimodal",
                )

    return await _do_call()


async def call_multimodal_chat(
    text: str,
    image_paths: list[str],
    model: str | None = None,
    api_key: str | None = None,
    provider: str = "dashscope",
    max_tokens: int = 2048,
    temperature: float = 0.1,
    mock_module: str | None = "multimodal_chat_response",
) -> str:
    """通用多模态图文对话入口。

    支持 DashScope (qwen-vl-plus/max) 和 OpenAI 兼容接口 (GPT-4o 等)。
    自动处理重试、追踪和 Mock 降级。
    """
    settings = get_settings()
    model = model or settings.llm.multimodal_model_name or settings.aliyun.vision_model

    if provider == "dashscope":
        content = []
        for path in image_paths:
            with open(path, "rb") as f:
                content.append({"image": f.read()})
        content.append({"text": text})
        return await call_dashscope_multimodal(
            model=model,
            messages=[{"role": "user", "content": content}],
            api_key=api_key or settings.aliyun.access_key_secret,
            mock_module=mock_module,
        )
    elif provider == "openai":
        user_content = []
        for path in image_paths:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            ext = Path(path).suffix.lstrip(".").lower()
            mime = f"image/{ext}" if ext in {"png", "jpg", "jpeg", "gif", "webp"} else "image/jpeg"
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}
            })
        user_content.append({"type": "text", "text": text})
        return await call_openai_chat(
            model=model,
            messages=[{"role": "user", "content": user_content}],
            api_key=api_key or settings.llm.multimodal_api_key or settings.llm.agent_api_key,
            base_url=settings.llm.multimodal_api_base,
            max_tokens=max_tokens,
            temperature=temperature,
            mock_module=mock_module,
        )
    else:
        raise ValueError(f"不支持的多模态 provider: {provider}")


# ─── LangChain ChatOpenAI 调用 ────────────────────────────────



async def call_langchain_chat(
    model: str,
    messages: List[Any],
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    temperature: float = 0.3,
    timeout: int = 120,
    mock_module: Optional[str] = None,
    **kwargs: Any,
) -> str:
    """调用 LangChain ChatOpenAI 接口。

    Args:
        model: 模型名称
        messages: LangChain 消息列表（HumanMessage, SystemMessage 等）
        api_key: API 密钥
        base_url: API 基础 URL
        temperature: 温度参数
        timeout: 超时时间（秒）
        mock_module: Mock 数据模块名称
        **kwargs: 其他参数

    Returns:
        响应文本内容
    """
    from langchain_openai import ChatOpenAI

    settings = get_settings()
    safe_key = api_key or settings.llm.agent_api_key or "sk-placeholder"
    safe_base = base_url or settings.llm.agent_api_base

    llm = ChatOpenAI(
        model=model,
        temperature=temperature,
        openai_api_base=safe_base,
        openai_api_key=safe_key,
        request_timeout=timeout,
        **kwargs,
    )

    @with_llm_retry(mock_module=mock_module)
    async def _do_call() -> str:
        with _trace_llm_call("llm_langchain_chat", model, "langchain_openai"):
            response = await llm.ainvoke(messages)
            return response.content if hasattr(response, "content") else str(response)

    return await _do_call()


__all__ = [
    "is_llm_configured",
    "call_openai_chat",
    "call_openai_embedding",
    "call_dashscope",
    "call_dashscope_multimodal",
    "call_multimodal_chat",
    "call_langchain_chat",
    "with_llm_retry",
]

