"""语音服务 - ASR（语音识别）Provider。

支持 DashScope Paraformer、OpenAI Whisper、edge-tts 本地方案。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from app.core.logger import get_logger

logger = get_logger(__name__)


class ASRProvider(ABC):
    """ASR Provider 抽象基类。"""

    @abstractmethod
    async def transcribe(
        self, audio_path: str, language: str = "zh"
    ) -> str:
        """语音转文字。

        Args:
            audio_path: 音频文件路径
            language: 语言代码

        Returns:
            识别文本
        """
        ...


class DashScopeASRProvider(ASRProvider):
    """DashScope Paraformer 语音识别。"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")

    async def transcribe(self, audio_path: str, language: str = "zh") -> str:
        """使用 DashScope Paraformer 进行语音识别。"""
        import aiohttp

        url = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        data = aiohttp.FormData()
        data.add_field("model", "paraformer-v2")
        data.add_field(
            "file",
            open(audio_path, "rb"),
            filename=Path(audio_path).name,
            content_type="audio/wav",
        )
        data.add_field("language_hints", [language])

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, data=data, timeout=60
                ) as resp:
                    result = await resp.json()
                    if resp.status == 200:
                        text = (
                            result.get("output", {})
                            .get("transcription", {})
                            .get("text", "")
                        )
                        return text or ""
                    else:
                        logger.error(f"ASR API 错误: {result}")
                        raise Exception(
                            f"ASR API 返回错误: {result.get('message', '未知错误')}"
                        )
        except Exception as e:
            logger.error(f"DashScope ASR 失败: {e}")
            raise


class OpenAIWhisperProvider(ASRProvider):
    """OpenAI Whisper API 语音识别。"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    async def transcribe(self, audio_path: str, language: str = "zh") -> str:
        """使用 OpenAI Whisper API 进行语音识别。"""
        from openai import AsyncOpenAI

        from app.core.settings import get_settings

        settings = get_settings()
        client = AsyncOpenAI(
            api_key=self.api_key or settings.llm.agent_api_key or "sk-placeholder",
            base_url=settings.llm.agent_api_base,
            timeout=60,
        )

        with open(audio_path, "rb") as f:
            transcription = await client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language=language,
            )

        return transcription.text


class ASRService:
    """ASR 服务门面。

    支持多种 ASR Provider 切换。
    """

    def __init__(self, provider: str = "dashscope"):
        from app.core.settings import get_settings

        settings = get_settings()
        self._providers = {
            "dashscope": DashScopeASRProvider(
                api_key=settings.llm.agent_api_key or os.getenv("DASHSCOPE_API_KEY")
            ),
            "openai": OpenAIWhisperProvider(api_key=settings.llm.agent_api_key),
        }
        self._default_provider = provider

    async def transcribe(
        self, audio_path: str, language: str = "zh", provider: Optional[str] = None
    ) -> str:
        """语音转文字。

        Args:
            audio_path: 音频文件路径
            language: 语言代码
            provider: ASR Provider 名称

        Returns:
            识别文本
        """
        provider_name = provider or self._default_provider
        asr = self._providers.get(provider_name)
        if asr is None:
            raise ValueError(f"不支持的 ASR Provider: {provider_name}")

        return await asr.transcribe(audio_path, language)


# 全局单例
_asr_service: Optional[ASRService] = None


def get_asr_service() -> ASRService:
    """获取全局 ASR 服务单例。"""
    global _asr_service
    if _asr_service is None:
        _asr_service = ASRService()
    return _asr_service
