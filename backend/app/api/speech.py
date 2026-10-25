"""语音输入端点（F2 · 浏览器端语音输入）。

两条通路，前端按 `/speech/status` 返回的 `mode` 选择：

| mode | 端点 | 说明 |
|---|---|---|
| `sts` | `POST /speech/sts-token` | 火山引擎 STS：前端用 STS token 直连火山 WebSocket 流式识别 |
| `transcribe` | `POST /speech/transcribe` | 后端 ASR 代理：浏览器录音 POST 上来，后端转写后返回文本 |

## 为什么密钥必须留在后端

STS token 的换取需要 `access_key`，ASR 代理需要云厂商 API Key。若前端直接换，
密钥必然进前端产物——任何人打开 DevTools 就能拿到，等于把云账号凭据公开。
doc-qa 的做法是后端换、前端只拿 token，本模块保持一致。

## 错误语义

| 场景 | HTTP | 错误码 | 含义 |
|---|---|---|---|
| 未配置密钥 | 503 | 3803 | 能力不可用（前端据此禁用按钮） |
| 上游 4xx | 502 | 3803 | 鉴权/参数问题，对用户不可读 |
| 上游 5xx / 网络 | 502 | 5201 | 依赖不可用 |
"""
import logging
import uuid

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel

from app.core.config import Settings
from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, get_trace_id, now_ms, set_trace_id
from app.dependencies import get_app_settings, get_principal
from app.services.security.auth import Principal
from app.services.speech.asr import build_transcriber, pcm_to_wav

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/speech", tags=["speech"])

#: 火山引擎 STS 换取地址
STS_URL = "https://openspeech.bytedance.com/api/v1/sts"
#: STS 资源 ID（语音 SaaS）
STS_RESOURCE_ID = "volcano.speech.saas.sts"
#: 裸 PCM 的 MIME（前端默认录这个格式，避免 webm 上游不收）
PCM_MIME = "audio/pcm"


class SpeechConfig(BaseModel):
    """STS 换取参数（与前端 LabASR 的 config 结构对齐）。"""

    appid: str
    token: str
    cluster: str
    duration: int


def _build_sts_payload(settings: Settings, user_id: str) -> dict:
    return {
        "appid": settings.volc_appid,
        "user": {"uid": user_id},
        "audio": {
            "format": "pcm",
            "codec": "pcm",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "reqid": str(uuid.uuid4()),
            "sequence": 1,
            "nbest": 1,
            "workflow": "speech_recognition",
            "result_type": "full",
            "enable_itn": True,
            "enable_punc": True,
        },
    }


def _build_sts_headers(settings: Settings) -> dict:
    return {
        "X-Api-App-Key": settings.volc_appid,
        "X-Api-Access-Key": settings.volc_access_key,
        "X-Api-Resource-Id": STS_RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Connect-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


@router.post("/sts-token", response_model=Envelope)
def sts_token(
    settings: Settings = Depends(get_app_settings),
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """换取语音识别 STS token（密钥不出后端）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()

    if not settings.volc_appid or not settings.volc_access_key:
        raise ApiError(
            ErrCode.SPEECH_UNAVAILABLE,
            "语音服务未配置",
            http_status=503,
        )

    import httpx  # 局部导入：未配置语音时不必付出导入成本

    try:
        resp = httpx.post(
            STS_URL,
            json=_build_sts_payload(settings, principal.user_id),
            headers=_build_sts_headers(settings),
            timeout=settings.speech_sts_timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 - 上游不可用 → 依赖不可用
        logger.warning("STS token 换取失败（网络）: %s", exc)
        raise ApiError(
            ErrCode.DEPENDENCY_UNAVAILABLE,
            "语音服务暂时不可用，请稍后再试",
            http_status=502,
        ) from exc

    if resp.status_code >= 500:
        logger.warning("STS token 换取失败（上游 %s）", resp.status_code)
        raise ApiError(
            ErrCode.DEPENDENCY_UNAVAILABLE,
            "语音服务暂时不可用，请稍后再试",
            http_status=502,
        )
    if resp.status_code >= 400:
        # 4xx 多为鉴权/参数问题：不给前端回显上游原文（可能含账号信息）
        logger.warning("STS token 换取被拒（上游 %s）: %s", resp.status_code, resp.text[:200])
        raise ApiError(
            ErrCode.SPEECH_UNAVAILABLE,
            "语音服务不可用（凭据或参数有误）",
            http_status=502,
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise ApiError(
            ErrCode.DEPENDENCY_UNAVAILABLE, "语音服务返回异常", http_status=502
        ) from exc

    token = (data.get("result") or {}).get("token")
    if not token:
        logger.warning("STS 响应中没有 token: %s", str(data)[:200])
        raise ApiError(
            ErrCode.DEPENDENCY_UNAVAILABLE, "语音服务返回异常", http_status=502
        )

    return ok_envelope(
        {
            "appid": settings.volc_appid,
            "token": token,
            "cluster": settings.volc_speech_cluster,
            "duration": settings.speech_sts_duration_s,
        },
        trace_id,
        elapsed_ms_since(started),
    )


@router.post("/transcribe", response_model=Envelope)
async def transcribe(
    file: UploadFile = File(..., description="录音文件（wav/mp3/m4a/... 或裸 PCM）"),
    sample_rate: int = Form(16000, description="裸 PCM 的采样率"),
    settings: Settings = Depends(get_app_settings),
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """把录音转成文字（后端代理云厂商 ASR）。

    浏览器录音默认是 **16kHz/单声道/16bit 裸 PCM**（`audio/pcm`）：
    Chrome 的 MediaRecorder 只能产出 webm/opus，而云端 ASR 普遍不收 webm；
    PCM 是各端都稳定支持的中间格式，后端包一层 WAV 头即可（见 `pcm_to_wav`）。
    也接受已经是容器格式的音频（wav/mp3/m4a/ogg…），直接透传给上游。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()

    transcriber = build_transcriber(settings)
    if transcriber is None:
        raise ApiError(
            ErrCode.SPEECH_UNAVAILABLE,
            "语音转写服务未配置",
            http_status=503,
        )

    limit = max(1, settings.speech_max_audio_mb) * 1024 * 1024
    # 先按 multipart 解析出的长度拒掉超大文件：否则会把整段音频读进内存才发现超限
    if file.size is not None and file.size > limit:
        raise ApiError(
            ErrCode.FILE_TOO_LARGE,
            f"录音超过 {settings.speech_max_audio_mb}MB 上限",
            http_status=400,
        )
    audio = await file.read()
    if not audio:
        raise ApiError(ErrCode.INVALID_INPUT, "录音内容为空", http_status=400)
    if len(audio) > limit:
        raise ApiError(
            ErrCode.FILE_TOO_LARGE,
            f"录音超过 {settings.speech_max_audio_mb}MB 上限",
            http_status=400,
        )

    filename = file.filename or "audio.pcm"
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    is_raw_pcm = content_type in {PCM_MIME, "audio/l16", "audio/x-pcm"} or filename.endswith(".pcm")

    if is_raw_pcm:
        audio = pcm_to_wav(audio, sample_rate=sample_rate)
        filename = "audio.wav"
        content_type = "audio/wav"

    result = transcriber.transcribe(
        audio,
        filename=filename,
        content_type=content_type,
        sample_rate=sample_rate,
    )
    logger.info(
        "语音转写完成 user=%s provider=%s model=%s bytes=%d ms=%d",
        principal.user_id,
        result.provider,
        result.model,
        len(audio),
        result.duration_ms,
    )
    return ok_envelope(
        {
            "text": result.text,
            "language": result.language,
            "provider": result.provider,
            "model": result.model,
            "duration_ms": result.duration_ms,
        },
        trace_id,
        elapsed_ms_since(started),
    )


@router.get("/status", response_model=Envelope)
def speech_status(settings: Settings = Depends(get_app_settings)) -> Envelope:
    """语音能力探测：前端据此决定按钮是否可点、用哪条通路。

    `mode`：`sts`（火山 STS 直连）/ `transcribe`（后端代理）/ `none`（不可用）。
    优先级：STS 配了就用 STS（流式、延迟低），否则用后端代理。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    sts_ready = bool(settings.volc_appid and settings.volc_access_key)
    transcriber = build_transcriber(settings)
    if sts_ready:
        mode = "sts"
    elif transcriber is not None:
        mode = "transcribe"
    else:
        mode = "none"
    return ok_envelope(
        {
            "available": mode != "none",
            "mode": mode,
            "cluster": settings.volc_speech_cluster,
            "provider": transcriber.name if transcriber else None,
            "model": getattr(transcriber, "_model", None) if transcriber else None,
        },
        trace_id,
        elapsed_ms_since(started),
    )
