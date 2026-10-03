"""音频解析器（阶段 3 §3.4）：语音 → 带时间戳文本。

- 生产：千问 paraformer-v2 语音识别。平台文件转写接口要求可访问的文件 URL；
  本地文件直传不在本阶段支持范围，会返回明确的错误说明（诚实失败而非静默入库）；
- 测试：MockTranscriber，返回带时间戳的合成转写文本；
- 转写文本按时间戳分段进向量库，音频本体进资源目录——两者分开存放；
- 时间戳是"答案跳转到视频对应位置"交互的基础，source 字段保留时间段。
"""
import hashlib
import logging
from pathlib import Path
from typing import Protocol

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import MediaAsset
from app.services.parsers.base import ParseResult

logger = logging.getLogger(__name__)


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> list[tuple[str, str]]:
        """返回 [(时间段 source, 文本)]，如 ("[0.0s-2.5s]", "你好")。"""
        ...


class ParaformerTranscriber:
    """千问 paraformer-v2 文件转写（要求可访问 URL）。"""

    def __init__(self, api_key: str, model: str = "paraformer-v2"):
        self._api_key = api_key
        self._model = model

    def transcribe(self, audio_path: Path) -> list[tuple[str, str]]:
        # paraformer-v2 文件转写接口要求 http(s) 可访问的文件 URL；
        # 本阶段未接入对象存储外链，诚实失败并给出明确说明（不静默入库）
        raise ApiError(
            ErrCode.DOCUMENT_PARSE_FAILED,
            "语音转写需要可访问的文件 URL（paraformer-v2 不支持本地文件直传）。"
            "请先将音频上传至对象存储，或使用 Mock 模式验证链路。",
        )


class MockTranscriber:
    """确定性假转写：带时间戳的合成文本。"""

    def transcribe(self, audio_path: Path) -> list[tuple[str, str]]:
        return [
            ("[0.0s-3.2s]", f"大家好，这是一段来自 {audio_path.name} 的模拟语音转写。"),
            ("[3.2s-7.8s]", "本转写由 Mock 引擎生成，用于无凭据环境下的全链路测试。"),
            ("[7.8s-12.0s]", "实际使用时由千问 paraformer-v2 返回带时间戳的真实转写文本。"),
        ]


class AudioParser:
    name = "audio"

    def __init__(self, transcriber: Transcriber, assets_dir: Path):
        self._transcriber = transcriber
        self._assets_dir = assets_dir

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        if not policy.get("transcribe_enabled", True):
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "未启用语音转写策略，音频文件无法入库")
        segments = self._transcriber.transcribe(path)
        if not segments:
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "语音转写结果为空")

        content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        self._assets_dir.mkdir(parents=True, exist_ok=True)
        asset_path = self._assets_dir / f"{doc_id}{path.suffix.lower()}"
        if asset_path.resolve() != path.resolve():
            asset_path.write_bytes(path.read_bytes())
        asset = MediaAsset(
            asset_id=f"{doc_id}#aud-1",
            doc_id=doc_id,
            uri=str(asset_path),
            caption=segments[0][1][:100],
            hash=content_hash,
            tenant_id=tenant_id,
            kind="audio",
        )
        # 转写文本按时间戳分段（source 保留时间段，供"跳转到视频位置"交互）
        return ParseResult(segments=[(f"{src}", text) for src, text in segments], assets=[asset])


def build_transcriber(provider: str, api_key: str) -> Transcriber:
    if provider == "dashscope":
        return ParaformerTranscriber(api_key=api_key)
    logger.info("使用 MockTranscriber（未配置千问 Key 或显式指定 mock）")
    return MockTranscriber()
