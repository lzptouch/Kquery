"""解析器路由（阶段 3 §3.1 + F3 扩展格式）：按扩展名分发，新增格式只需注册一个 Parser。

不支持的扩展名 → 明确报错（上传前前端已拦截，此处为兜底）。
"""
import inspect
import logging
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.services.parsers.audio import AudioParser
from app.services.parsers.base import ParseResult
from app.services.parsers.docx import DocxParser
from app.services.parsers.markdown import MarkdownParser
from app.services.parsers.ocr import OcrParser
from app.services.parsers.pdf import PdfParser
from app.services.parsers.plaintext import CODE_EXTENSIONS, HTML_EXTENSIONS, TEXT_EXTENSIONS, PlainTextParser
from app.services.parsers.presentation import PresentationParser
from app.services.parsers.spreadsheet import SpreadsheetParser

logger = logging.getLogger(__name__)

#: 纯文本类扩展名（txt/csv/json/yaml/日志/配置）
PLAIN_EXTENSIONS = set(TEXT_EXTENSIONS) | set(HTML_EXTENSIONS)
#: 代码文件扩展名
CODE_EXTS = set(CODE_EXTENSIONS)

SUPPORTED_EXTENSIONS = {
    ".md", ".markdown", ".pdf", ".docx", ".doc",
    ".png", ".jpg", ".jpeg", ".mp3", ".wav",
    # F3 新增
    ".xlsx",
    # F9：PPT/PPTX（补齐 doc-qa 的演示文稿能力）
    ".pptx",
    *PLAIN_EXTENSIONS,
    *CODE_EXTS,
}

#: 明确不支持但用户常会误传的扩展名 → 给可读错误（而不是笼统的"不支持"）
UNSUPPORTED_HINTS = {
    ".xls": "不支持旧版 Excel 格式（.xls），请另存为 .xlsx 后重试",
    ".ppt": "不支持旧版 PowerPoint（.ppt），请另存为 .pptx 后重试",
}

# 文件类型展示分组（前端上传向导按此分栏）
TYPE_GROUPS = {
    "document": {
        ".md", ".markdown", ".pdf", ".docx", ".doc", ".xlsx", ".pptx",
        *PLAIN_EXTENSIONS,
    },
    "code": set(CODE_EXTS),
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
        spreadsheet_parser: SpreadsheetParser | None = None,
        plaintext_parser: PlainTextParser | None = None,
        presentation_parser: PresentationParser | None = None,
    ):
        self._parsers = {
            ".md": MarkdownParser(),
            ".markdown": MarkdownParser(),
            ".pdf": pdf_parser or PdfParser(),
            ".docx": docx_parser,
            ".doc": docx_parser,
            ".xlsx": spreadsheet_parser or SpreadsheetParser(),
            ".pptx": presentation_parser or PresentationParser(),
        }
        # 纯文本与代码文件共用一个解析器实例（无状态，安全共享）
        text_parser = plaintext_parser or PlainTextParser()
        for ext in PLAIN_EXTENSIONS | CODE_EXTS:
            self._parsers[ext] = text_parser
        self._ocr = ocr_parser
        self._audio = audio_parser

    def supported_extensions(self) -> set[str]:
        """已注册且可路由的扩展名集合（健康检查探针用）。"""
        return set(SUPPORTED_EXTENSIONS)

    def route(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in self._parsers:
            return self._parsers[ext].name
        if ext in TYPE_GROUPS["image"]:
            return "ocr"
        if ext in TYPE_GROUPS["audio"]:
            return "audio"
        raise ApiError(ErrCode.UNSUPPORTED_FILE_TYPE, self._unsupported_message(ext))

    @staticmethod
    def _unsupported_message(ext: str) -> str:
        """不支持的扩展名 → 可读错误（常见误传格式给专门提示）。"""
        hint = UNSUPPORTED_HINTS.get(ext)
        if hint:
            return hint
        return f"不支持的文件类型 {ext}（支持：{sorted(SUPPORTED_EXTENSIONS)}）"

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict, progress=None) -> ParseResult:
        """路由解析。`progress(done, total)` 为可选的阶段内进度回调。

        只在解析器**显式支持**时透传（用签名探测）：给所有解析器硬塞一个
        `progress=` 会让既有实现与测试替身全部因签名不匹配而崩。
        """
        ext = path.suffix.lower()
        if ext in self._parsers:
            parser = self._parsers[ext]
        elif ext in TYPE_GROUPS["image"]:
            parser = self._ocr
        elif ext in TYPE_GROUPS["audio"]:
            parser = self._audio
        else:
            raise ApiError(ErrCode.UNSUPPORTED_FILE_TYPE, self._unsupported_message(ext))
        result = self._invoke(parser, path, doc_id, tenant_id, policy, progress)
        logger.info(
            "解析完成: %s（parser=%s, 段=%d, 表格=%d, 资源=%d, 警告=%d）",
            path.name, parser.name, len(result.segments), len(result.tables),
            len(result.assets), len(result.warnings),
        )
        return result

    @staticmethod
    def _invoke(parser, path: Path, doc_id: str, tenant_id: str, policy: dict, progress):
        if progress is None:
            return parser.parse(path, doc_id, tenant_id, policy)
        try:
            params = inspect.signature(parser.parse).parameters
        except (TypeError, ValueError):
            return parser.parse(path, doc_id, tenant_id, policy)
        if "progress" in params:
            return parser.parse(path, doc_id, tenant_id, policy, progress=progress)
        return parser.parse(path, doc_id, tenant_id, policy)
