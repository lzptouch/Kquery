"""AI 写作服务。

封装续写、润色、总结、翻译等 AI 写作功能，
支持流式和非流式输出。
"""

from __future__ import annotations

from typing import AsyncGenerator, Optional

from app.core.llm_client import call_openai_chat
from app.core.llm.router import resolve_provider_config
from app.core.logger import get_logger
from app.services.writing_prompts import WRITING_ACTIONS

logger = get_logger(__name__)


class WritingService:
    """AI 写作服务。

    提供续写、润色、总结、翻译等 AI 写作方法，
    支持按模型名切换底层 LLM。

    使用方式：
        service = WritingService()
        result = await service.continue_writing(text="...", style="专业")
    """

    def __init__(self, model: Optional[str] = None):
        """初始化写作服务。

        Args:
            model: 使用的模型名称，为 None 使用默认模型
        """
        self.model = model
        provider_config = resolve_provider_config(model)
        self._api_key = provider_config["api_key"]
        self._base_url = provider_config["base_url"]
        self._model_name = provider_config["model"]

    async def continue_writing(
        self, text: str, style: str = "专业正式"
    ) -> str:
        """续写文本。

        Args:
            text: 上文内容
            style: 写作风格描述

        Returns:
            续写内容
        """
        action = WRITING_ACTIONS["continue"]
        system_prompt = action["system"].format(style=style)
        user_prompt = action["user"].format(text=text)

        return await call_openai_chat(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=0.7,
            max_tokens=2048,
        )

    async def polish(self, text: str, tone: str = "简洁有力") -> str:
        """润色文本。

        Args:
            text: 原文
            tone: 润色风格

        Returns:
            润色后文本
        """
        action = WRITING_ACTIONS["polish"]
        system_prompt = action["system"].format(tone=tone)
        user_prompt = action["user"].format(text=text)

        return await call_openai_chat(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=0.5,
            max_tokens=2048,
        )

    async def summarize(self, text: str) -> str:
        """总结文本。

        Args:
            text: 原文

        Returns:
            总结内容
        """
        action = WRITING_ACTIONS["summarize"]
        system_prompt = action["system"]
        user_prompt = action["user"].format(text=text)

        return await call_openai_chat(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=0.3,
            max_tokens=1024,
        )

    async def translate(self, text: str, target_lang: str = "英文") -> str:
        """翻译文本。

        Args:
            text: 原文
            target_lang: 目标语言

        Returns:
            翻译结果
        """
        action = WRITING_ACTIONS["translate"]
        system_prompt = action["system"].format(target_lang=target_lang)
        user_prompt = action["user"].format(text=text)

        return await call_openai_chat(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=0.3,
            max_tokens=2048,
        )

    async def generate_title(self, text: str) -> str:
        """根据内容生成标题。

        Args:
            text: 内容文本

        Returns:
            生成的标题
        """
        action = WRITING_ACTIONS["generate_title"]
        system_prompt = action["system"]
        user_prompt = action["user"].format(text=text)

        return await call_openai_chat(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=0.5,
            max_tokens=100,
        )

    async def generate_outline(self, topic: str) -> str:
        """根据主题生成大纲。

        Args:
            topic: 主题

        Returns:
            Markdown 格式大纲
        """
        action = WRITING_ACTIONS["generate_outline"]
        system_prompt = action["system"]
        user_prompt = action["user"].format(topic=topic)

        return await call_openai_chat(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            api_key=self._api_key,
            base_url=self._base_url,
            temperature=0.5,
            max_tokens=2048,
        )

    async def stream_action(
        self, action: str, text: str, **kwargs
    ) -> AsyncGenerator[str, None]:
        """流式执行写作操作。

        Args:
            action: 操作类型 (continue/polish/summarize/translate)
            text: 文本内容
            **kwargs: 额外参数 (style, tone, target_lang 等)

        Yields:
            流式文本片段
        """
        from openai import AsyncOpenAI

        action_config = WRITING_ACTIONS.get(action)
        if not action_config:
            raise ValueError(f"不支持的写作操作: {action}")

        system_prompt = action_config["system"].format(**kwargs)
        user_prompt = action_config["user"].format(text=text, **kwargs)

        client = AsyncOpenAI(
            api_key=self._api_key or "sk-placeholder",
            base_url=self._base_url,
            timeout=120,
        )

        stream = await client.chat.completions.create(
            model=self._model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3 if action in ("summarize", "translate") else 0.7,
            max_tokens=2048,
            stream=True,
        )

        async for chunk in stream:
            if chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
