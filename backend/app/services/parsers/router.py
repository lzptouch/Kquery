"""解析器路由（阶段 3 §3.1）：按扩展名分发，新增格式只需注册一个 Parser。

不支持的扩展名 → 明确报错（上传前前端已拦截，此处为兜底）。
"""
import logging
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.services.parsers.audio import AudioParser
from app.services.parsers.base import ParseResult
from app.services.parsers.docx import DocxParser
from app.services.parsers.markdown import MarkdownParser
from app.services.parsers.ocr import OcrParser
from app.services.parsers.pdf import PdfParser

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {
    ".md", ".markdown", ".pdf", ".docx", ".doc",
    ".png", ".jpg", ".jpeg", ".mp3", ".wav",
}

# 文件类型展示分组（前端上传向导按此分栏）
TYPE_GROUPS = {
    "document": {".md", ".markdown", ".pdf", ".docx", ".doc"},
    "image": {".png", ".jpg", ".jpeg"},
    "audio": {".mp3", ".wav"},
}


class ParserRouter:
    def __init__(
        self,
        ocr_parser: OcrParser,
        audio_parser: AudioParser,
        docx_parser: DocxParser,
        pdf_parser: PdfParser | None = None,
    ):
        self._parsers = {
            ".md": MarkdownParser(),
            ".markdown": MarkdownParser(),
            ".pdf": pdf_parser or PdfParser(),
            ".docx": docx_parser,
            ".doc": docx_parser,
        }
        self._ocr = ocr_parser
        self._audio = audio_parser

    def route(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in self._parsers:
            return self._parsers[ext].name
        if ext in TYPE_GROUPS["image"]:
            return "ocr"
        if ext in TYPE_GROUPS["audio"]:
            return "audio"
        raise ApiError(
            ErrCode.UNSUPPORTED_FILE,
            f"不支持的文件类型 {ext}（支持：{sorted(SUPPORTED_EXTENSIONS)}）",
        )

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        ext = path.suffix.lower()
        if ext in self._parsers:
            parser = self._parsers[ext]
        elif ext in TYPE_GROUPS["image"]:
            parser = self._ocr
        elif ext in TYPE_GROUPS["audio"]:
            parser = self._audio
        else:
            raise ApiError(
                ErrCode.UNSUPPORTED_FILE,
                f"不支持的文件类型 {ext}（支持：{sorted(SUPPORTED_EXTENSIONS)}）",
            )
        result = parser.parse(path, doc_id, tenant_id, policy)
        logger.info(
            "解析完成: %s（parser=%s, 段=%d, 表格=%d, 资源=%d, 警告=%d）",
            path.name, parser.name, len(result.segments), len(result.tables),
            len(result.assets), len(result.warnings),
        )
        return result
