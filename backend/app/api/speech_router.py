"""语音相关 API 路由 - ASR/TTS。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/speech", tags=["语音能力"])


class TTSRequest(BaseModel):
    """TTS 请求。"""
    text: str = Field(description="待合成的文本")
    voice: str = Field(default="default", description="音色")
    speed: float = Field(default=1.0, ge=0.5, le=2.0, description="语速")


@router.post("/asr")
async def speech_to_text(
    file: UploadFile = File(description="音频文件"),
    language: str = Form(default="zh", description="语言代码"),
):
    """语音转文字（ASR）。

    支持 wav, mp3, ogg, m4a, amr 格式。

    Args:
        file: 音频文件
        language: 语言代码
    """
    import shutil
    import uuid
    from pathlib import Path

    from app.core.settings import get_settings
    from app.services.speech.asr_service import get_asr_service

    # 校验格式
    allowed_formats = {"wav", "mp3", "ogg", "m4a", "amr", "webm", "flac"}
    ext = Path(file.filename or "audio.wav").suffix.lstrip(".").lower()
    if ext not in allowed_formats:
        raise HTTPException(status_code=400, detail=f"不支持的音频格式: {ext}")

    # 保存文件
    settings = get_settings()
    upload_dir = Path(settings.storage.upload_dir) / "voice"
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = upload_dir / f"{uuid.uuid4()}.{ext}"

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        asr_service = get_asr_service()
        text = await asr_service.transcribe(str(file_path), language)

        if not text:
            raise HTTPException(status_code=422, detail="语音识别未返回文本")

        return {
            "success": True,
            "text": text,
            "language": language,
            "file_name": file.filename,
        }
    except Exception as e:
        logger.error(f"ASR 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"语音识别失败: {str(e)}")


@router.post("/tts")
async def text_to_speech(request: TTSRequest):
    """文本转语音（TTS）。

    返回音频流（audio/mpeg）。

    Args:
        request: TTS 请求
    """
    from app.services.speech.tts_service import get_tts_service

    try:
        tts_service = get_tts_service()
        audio_data = await tts_service.synthesize(
            text=request.text,
            voice=request.voice,
            speed=request.speed,
        )

        return Response(
            content=audio_data,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": 'attachment; filename="speech.mp3"',
            },
        )
    except Exception as e:
        logger.error(f"TTS 失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"语音合成失败: {str(e)}")
