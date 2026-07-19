"""LLM 多模型切换 API 路由。

提供模型列表查询、按模型名聊天等接口。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.llm.registry import get_llm_registry
from app.core.llm.router import resolve_provider, resolve_provider_config
from app.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["LLM 多模型"])


class ModelInfoResponse(BaseModel):
    """模型信息响应。"""
    name: str
    display_name: str
    provider_type: str
    model: str
    capabilities: list[str]
    is_default: bool
    temperature: float
    max_tokens: int
    description: str


class ModelsListResponse(BaseModel):
    """模型列表响应。"""
    models: list[ModelInfoResponse]
    default_model: Optional[str] = None
    total: int


class ChatWithModelRequest(BaseModel):
    """按模型聊天请求。"""
    model: Optional[str] = Field(default=None, description="模型名称，为 None 使用默认模型")
    messages: list[dict] = Field(description="消息列表")
    temperature: Optional[float] = Field(default=None, description="温度参数")
    max_tokens: Optional[int] = Field(default=None, description="最大 token 数")
    stream: bool = Field(default=False, description="是否流式返回")


@router.get("/models", response_model=ModelsListResponse)
async def list_models():
    """获取所有可用模型列表。

    Returns:
        模型列表、默认模型和总数
    """
    registry = get_llm_registry()
    models = registry.list_models()
    default = registry.get_default()

    return ModelsListResponse(
        models=[ModelInfoResponse(**m) for m in models],
        default_model=default.name if default else None,
        total=len(models),
    )


@router.get("/models/{model_name}")
async def get_model(model_name: str):
    """获取指定模型的详细信息。

    Args:
        model_name: 模型名称

    Returns:
        模型详细信息

    Raises:
        HTTPException 404: 模型不存在
    """
    provider = resolve_provider(model_name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"模型 '{model_name}' 不存在")

    return ModelInfoResponse(**provider.to_dict())


@router.post("/chat")
async def chat_with_model(request: ChatWithModelRequest):
    """使用指定模型进行聊天（非流式）。

    Args:
        request: 聊天请求

    Returns:
        聊天响应
    """
    provider = resolve_provider(request.model)
    if provider is None:
        raise HTTPException(
            status_code=400,
            detail=f"模型 '{request.model}' 不存在，请先通过 GET /models 查看可用模型",
        )

    try:
        from app.core.llm_client import call_openai_chat, call_dashscope

        temperature = request.temperature or provider.temperature
        max_tokens = request.max_tokens or provider.max_tokens

        if provider.provider_type == "dashscope":
            response = await call_dashscope(
                model=provider.model,
                messages=request.messages,
                api_key=provider.api_key,
                max_tokens=max_tokens,
                temperature=temperature,
                mock_module="chat_response",
            )
        else:
            response = await call_openai_chat(
                model=provider.model,
                messages=request.messages,
                api_key=provider.api_key,
                base_url=provider.base_url,
                max_tokens=max_tokens,
                temperature=temperature,
                mock_module="chat_response",
            )

        return {
            "success": True,
            "model": provider.name,
            "response": response,
        }
    except Exception as e:
        logger.error(f"模型调用失败 [{provider.name}]: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"模型调用失败: {str(e)}")


@router.get("/models/config")
async def get_models_config():
    """获取模型配置（供配置中心使用）。"""
    registry = get_llm_registry()
    return {
        "providers": registry.list_models(),
        "total": registry.count(),
    }
