"""语音转写服务（F2 · 后端 ASR 代理）。

## 为什么不让浏览器直连 ASR

浏览器直连需要把云厂商凭据或 STS token 暴露在前端（要么进产物，要么走明文 WS）。
后端代理的代价是多一次上传，但换来两件事：

- 凭据只在后端（密钥不进产物）；
- 厂商 SDK / 协议差异被收口在一处，换厂商不用动前端。

## 两条通路

| provider | 协议 | 说明 |
|---|---|---|
| `dashscope` | 阿里云百炼原生多模态接口 | 模型 `qwen3-asr-flash`（另有 `fun-asr-flash`）。音频以 base64 data URI 内联，无需公网可访问 URL |
| `openai` | OpenAI 兼容 `/v1/audio/transcriptions` | 任何兼容实现（OpenAI / Groq / 自建 whisper.cpp / faster-whisper-server） |

## 音频格式

浏览器端统一录 **16kHz / 单声道 / 16bit PCM**（见前端 `VoiceRecorder`），
后端用 `pcm_to_wav` 包一层 WAV 头。原因：Chrome 的 MediaRecorder 只能产出
`audio/webm;codecs=opus`，而云端 ASR 普遍不收 webm；PCM 是唯一各端都稳定支持的中间格式，
包 WAV 头只要几十字节代码，不值得为它往镜像里塞 ffmpeg。

## 错误语义

| 场景 | HTTP | 错误码 |
|---|---|---|
| 未配置转写后端 | 503 | 3803 SPEECH_UNAVAILABLE |
| 音频超限 | 400 | 2004 FILE_TOO_LARGE |
| 上游 4xx | 502 | 3803 |
| 上游 5xx / 网络 | 502 | 5201 DEPENDENCY_UNAVAILABLE |
"""
from __future__ import annotations

import base64
import logging
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.envelope import ApiError, ErrCode

logger = logging.getLogger(__name__)

#: 百炼原生多模态（ASR）端点
DASHSCOPE_ASR_URL = (
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
)
#: OpenAI 兼容转写端点
OPENAI_ASR_URL = "https://api.openai.com/v1/audio/transcriptions"

#: 容器格式 → 上游 input_audio_format
_AUDIO_FORMAT_BY_MIME = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/amr": "amr",
    "audio/pcm": "pcm",
    "audio/l16": "pcm",
}


@dataclass
class ASRResult:
    """一次转写的结果。"""

    text: str
    language: str | None = None
    provider: str = ""
    model: str = ""
    duration_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class Transcriber(Protocol):
    """转写器接口。"""

    name: str

    def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "audio.wav",
        content_type: str = "audio/wav",
        sample_rate: int = 16000,
    ) -> ASRResult: ...


def pcm_to_wav(pcm: bytes, sample_rate: int = 16000, channels: int = 1, bits: int = 16) -> bytes:
    """把裸 PCM 包成 WAV 容器（RIFF 头 44 字节）。

    只支持 16bit —— 浏览器 `AudioContext` 拿到的 Float32 转 Int16 之后就是 16bit，
    上游 ASR 也都收 16bit，多种位宽在这里加分支只会带来"某一支没测过"的风险。
    """
    if bits != 16:
        raise ValueError(f"仅支持 16bit PCM，收到 {bits}bit")
    byte_rate = sample_rate * channels * bits // 8
    block_align = channels * bits // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),          # RIFF 块大小
        b"WAVE",
        b"fmt ",
        16,                    # fmt 块大小
        1,                     # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        len(pcm),
    )
    return header + pcm


def guess_audio_format(filename: str, content_type: str) -> str:
    """推断上游认识的音频格式标识。"""
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime in _AUDIO_FORMAT_BY_MIME:
        return _AUDIO_FORMAT_BY_MIME[mime]
    lowered = (filename or "").lower()
    for ext, fmt in (
        (".wav", "wav"),
        (".mp3", "mp3"),
        (".m4a", "m4a"),
        (".mp4", "m4a"),
        (".aac", "aac"),
        (".ogg", "ogg"),
        (".flac", "flac"),
        (".amr", "amr"),
        (".pcm", "pcm"),
    ):
        if lowered.endswith(ext):
            return fmt
    return "wav"


class DashScopeTranscriber:
    """百炼原生 ASR（`qwen3-asr-flash` / `fun-asr-flash`）。

    音频以 base64 data URI 内联在请求体里——不需要公网可访问的 URL，
    这对"服务端本地文件"和"内网部署"至关重要。
    """

    name = "dashscope"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._model = settings.speech_asr_model or "qwen3-asr-flash"
        self._api_key = settings.speech_asr_api_key or settings.dashscope_api_key
        self._url = settings.speech_asr_base_url or DASHSCOPE_ASR_URL

    def _payload(self, audio: bytes, audio_format: str, sample_rate: int) -> dict:
        b64 = base64.b64encode(audio).decode("ascii")
        return {
            "model": self._model,
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"audio": f"data:audio/{audio_format};base64,{b64}"}],
                    }
                ]
            },
            "parameters": {
                "result_format": "message",
                "input_audio_format": audio_format,
                "sample_rate": sample_rate,
                "enable_itn": True,
            },
        }

    @staticmethod
    def _extract(data: dict) -> tuple[str, str | None]:
        """从百炼响应里取文本。

        响应结构：`output.choices[0].message.content[0].text`；
        老模型可能给字符串而不是对象列表，两种都要兜住。
        """
        output = data.get("output") or {}
        choices = output.get("choices") or []
        if not choices:
            return "", None
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip(), None
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    texts.append(item["text"])
            text = "".join(texts).strip()
            annotations = message.get("annotations") or []
            language = None
            for ann in annotations:
                if isinstance(ann, dict) and ann.get("language"):
                    language = ann["language"]
                    break
            return text, language
        return "", None

    def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "audio.wav",
        content_type: str = "audio/wav",
        sample_rate: int = 16000,
    ) -> ASRResult:
        audio_format = guess_audio_format(filename, content_type)
        started = time.perf_counter()
        try:
            resp = httpx.post(
                self._url,
                json=self._payload(audio, audio_format, sample_rate),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self._settings.speech_asr_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - 网络错误统一映射为依赖不可用
            logger.warning("ASR 调用失败（网络）: %s", exc)
            raise ApiError(
                ErrCode.DEPENDENCY_UNAVAILABLE,
                "语音转写服务暂时不可用，请稍后再试",
                http_status=502,
            ) from exc

        if resp.status_code >= 500:
            logger.warning("ASR 调用失败（上游 %s）", resp.status_code)
            raise ApiError(
                ErrCode.DEPENDENCY_UNAVAILABLE,
                "语音转写服务暂时不可用，请稍后再试",
                http_status=502,
            )
        if resp.status_code >= 400:
            # 4xx 多为鉴权/参数问题：不给前端回显上游原文（可能含账号信息）
            logger.warning("ASR 调用被拒（上游 %s）: %s", resp.status_code, resp.text[:200])
            raise ApiError(
                ErrCode.SPEECH_UNAVAILABLE,
                "语音转写服务不可用（凭据或参数有误）",
                http_status=502,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ApiError(
                ErrCode.DEPENDENCY_UNAVAILABLE, "语音转写服务返回异常", http_status=502
            ) from exc

        text, language = self._extract(data)
        if not text:
            logger.warning("ASR 返回空文本: %s", str(data)[:200])
            raise ApiError(
                ErrCode.DEPENDENCY_UNAVAILABLE, "语音转写服务返回异常", http_status=502
            )
        return ASRResult(
            text=text,
            language=language,
            provider=self.name,
            model=self._model,
            duration_ms=int((time.perf_counter() - started) * 1000),
            raw={"request_id": data.get("request_id")},
        )


class OpenAICompatTranscriber:
    """OpenAI 兼容转写端点（`POST {base}/audio/transcriptions`）。"""

    name = "openai"

    def __init__(self, settings: Settings):
        self._settings = settings
        self._model = settings.speech_asr_model or "whisper-1"
        self._api_key = settings.speech_asr_api_key or settings.dashscope_api_key
        self._url = settings.speech_asr_base_url or OPENAI_ASR_URL

    def transcribe(
        self,
        audio: bytes,
        *,
        filename: str = "audio.wav",
        content_type: str = "audio/wav",
        sample_rate: int = 16000,
    ) -> ASRResult:
        started = time.perf_counter()
        try:
            resp = httpx.post(
                self._url,
                files={"file": (filename, audio, content_type or "audio/wav")},
                data={"model": self._model},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._settings.speech_asr_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ASR 调用失败（网络）: %s", exc)
            raise ApiError(
                ErrCode.DEPENDENCY_UNAVAILABLE,
                "语音转写服务暂时不可用，请稍后再试",
                http_status=502,
            ) from exc

        if resp.status_code >= 500:
            raise ApiError(
                ErrCode.DEPENDENCY_UNAVAILABLE,
                "语音转写服务暂时不可用，请稍后再试",
                http_status=502,
            )
        if resp.status_code >= 400:
            logger.warning("ASR 调用被拒（上游 %s）: %s", resp.status_code, resp.text[:200])
            raise ApiError(
                ErrCode.SPEECH_UNAVAILABLE,
                "语音转写服务不可用（凭据或参数有误）",
                http_status=502,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise ApiError(
                ErrCode.DEPENDENCY_UNAVAILABLE, "语音转写服务返回异常", http_status=502
            ) from exc

        text = (data.get("text") or "").strip()
        if not text:
            raise ApiError(
                ErrCode.DEPENDENCY_UNAVAILABLE, "语音转写服务返回异常", http_status=502
            )
        return ASRResult(
            text=text,
            language=data.get("language"),
            provider=self.name,
            model=self._model,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def build_transcriber(settings: Settings) -> Transcriber | None:
    """按配置构造转写器；没配后端返回 None（前端据此禁用按钮）。"""
    provider = (settings.speech_asr_provider or "none").strip().lower()
    if provider in {"none", "", "off"}:
        return None
    if provider == "dashscope":
        if not (settings.speech_asr_api_key or settings.dashscope_api_key):
            logger.warning("speech_asr_provider=dashscope 但没有 API Key，语音转写不可用")
            return None
        return DashScopeTranscriber(settings)
    if provider == "openai":
        if not (settings.speech_asr_api_key or settings.dashscope_api_key):
            logger.warning("speech_asr_provider=openai 但没有 API Key，语音转写不可用")
            return None
        return OpenAICompatTranscriber(settings)
    logger.warning("未知的 speech_asr_provider=%s，语音转写不可用", provider)
    return None
