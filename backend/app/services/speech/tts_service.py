"""语音服务 - TTS（文本转语音）Provider。

支持 DashScope CosyVoice、OpenAI TTS 和 Edge TTS。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional

from app.core.logger import get_logger

logger = get_logger(__name__)


class TTSProvider(ABC):
    """TTS Provider 抽象基类。"""

    @abstractmethod
    async def synthesize(
        self, text: str, voice: str = "default", speed: float = 1.0
    ) -> bytes:
        """文本转语音。

        Args:
            text: 文本内容
            voice: 音色
            speed: 语速（0.5-2.0）

        Returns:
            音频字节数据
        """
        ...


class DashScopeCosyVoiceProvider(TTSProvider):
    """DashScope CosyVoice TTS（中文自然度高）。"""

    def __init__(self, api_key: Optional[str] = None):
        import os as _os

        self.api_key = api_key or _os.getenv("DASHSCOPE_API_KEY", "")

    async def synthesize(
        self, text: str, voice: str = "longxiaochun", speed: float = 1.0
    ) -> bytes:
        """使用 CosyVoice 合成语音。"""
        import aiohttp
        import json

        url = "https://dashscope.aliyuncs.com/api/v1/services/audio/tts/speech"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": "cosyvoice-v1",
            "input": {"text": text},
            "parameters": {
                "voice": voice,
                "speech_rate": int(speed * 100),
            },
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=headers, json=payload, timeout=30
                ) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    else:
                        error = await resp.text()
                        logger.error(f"TTS API 错误: {error}")
                        raise Exception(f"TTS API 返回错误: HTTP {resp.status}")
        except Exception as e:
            logger.error(f"CosyVoice TTS 失败: {e}")
            raise


class OpenAITTSProvider(TTSProvider):
    """OpenAI TTS API。"""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key

    async def synthesize(
        self, text: str, voice: str = "alloy", speed: float = 1.0
    ) -> bytes:
        """使用 OpenAI TTS 合成语音。"""
        from openai import AsyncOpenAI

        from app.core.settings import get_settings

        settings = get_settings()
        client = AsyncOpenAI(
            api_key=self.api_key or settings.llm.agent_api_key or "sk-placeholder",
            base_url=settings.llm.agent_api_base,
            timeout=30,
        )

        # text 长度限制
        if len(text) > 4096:
            text = text[:4093] + "..."

        response = await client.audio.speech.create(
            model="tts-1",
            voice=voice,
            input=text,
            speed=speed,
            response_format="mp3",
        )

        return response.read()


class EdgeTTSProvider(TTSProvider):
    """Edge TTS（免费，无需 API Key，适合开发）。"""

    async def synthesize(
        self, text: str, voice: str = "zh-CN-XiaoxiaoNeural", speed: float = 1.0
    ) -> bytes:
        """使用 Edge TTS 合成语音。"""
        import asyncio
        import io
        import tempfile

        try:
            import edge_tts

            rate = f"{'+' if speed >= 1.0 else ''}{int((speed - 1.0) * 100)}%"

            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp_path = tmp.name

            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await communicate.save(tmp_path)

            with open(tmp_path, "rb") as f:
                audio_data = f.read()

            os.unlink(tmp_path)
            return audio_data

        except ImportError:
            logger.warning("edge-tts 未安装，请 pip install edge-tts")
            raise


class TTSService:
    """TTS 服务门面。"""

    def __init__(self, provider: str = "edge_tts"):
        from app.core.settings import get_settings

        settings = get_settings()

        self._providers = {
            "dashscope": DashScopeCosyVoiceProvider(
                api_key=settings.llm.agent_api_key or os.getenv("DASHSCOPE_API_KEY")
            ),
            "openai": OpenAITTSProvider(api_key=settings.llm.agent_api_key),
            "edge_tts": EdgeTTSProvider(),
        }
        self._default_provider = provider

    async def synthesize(
        self,
        text: str,
        voice: str = "default",
        speed: float = 1.0,
        provider: Optional[str] = None,
    ) -> bytes:
        """文本转语音。

        Args:
            text: 文本内容
            voice: 音色
            speed: 语速
            provider: TTS Provider 名称

        Returns:
            音频字节数据
        """
        provider_name = provider or self._default_provider
        tts = self._providers.get(provider_name)
        if tts is None:
            raise ValueError(f"不支持的 TTS Provider: {provider_name}")

        return await tts.synthesize(text, voice, speed)


# 全局单例
_tts_service: Optional[TTSService] = None


def get_tts_service() -> TTSService:
    """获取全局 TTS 服务单例。"""
    global _tts_service
    if _tts_service is None:
        _tts_service = TTSService()
    return _tts_service
