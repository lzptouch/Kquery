"""解析器路由（阶段 3 §3.1 + F3 扩展格式）：按扩展名分发，新增格式只需注册一个 Parser。

不支持的扩展名 → 明确报错（上传前前端已拦截，此处为兜底）。
"""
import inspect
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.services.parsers.base import ParseResult
from app.services.parsers.docx import DocxParser
from app.services.parsers.markdown import MarkdownParser
from app.services.parsers.ocr import OcrParser
from app.services.parsers.pdf import PdfParser
from app.services.parsers.plaintext import CODE_EXTENSIONS, HTML_EXTENSIONS, TEXT_EXTENSIONS, PlainTextParser
from app.services.parsers.presentation import PresentationParser
from app.services.parsers.spreadsheet import SpreadsheetParser
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 纯文本类扩展名（txt/csv/json/yaml/日志/配置）
PLAIN_EXTENSIONS = set(TEXT_EXTENSIONS) | set(HTML_EXTENSIONS)
#: 代码文件扩展名
CODE_EXTS = set(CODE_EXTENSIONS)

SUPPORTED_EXTENSIONS = {
    ".md", ".markdown", ".pdf", ".docx", ".doc",
    ".png", ".jpg", ".jpeg",
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
    # 语音能力已下线：给明确提示，而不是让用户对着"不支持的文件类型"猜
    ".mp3": "本项目不含语音转写，音频请先转成文字（.txt/.md）后上传",
    ".wav": "本项目不含语音转写，音频请先转成文字（.txt/.md）后上传",
    ".m4a": "本项目不含语音转写，音频请先转成文字（.txt/.md）后上传",
}

#: 解析策略字段真源：键 → (允许的类型, 含义)
#:
#: 解析策略全是**开关**，而 Python 的真值语义会把类型错误的值悄悄接受：
#: `{"pdf_tables": "yes"}` 里的 "yes" 是非空字符串 → 真值 → 表格识别照常开启。
#: 用户以为自己关掉了某个能力，实际没关——最坏的情况是扫描件照样走云端 OCR，
#: 费用照收，而界面上的开关显示的是"已关闭"。
#:
#: 类型元组里显式列出 ``bool``：``bool`` 是 ``int`` 的子类，
#: 但反过来不成立——``isinstance(True, int)`` 为 True，
#: 所以布尔位必须单独判，否则 ``True`` 会被整数字段放行。
PARSE_POLICY_SCHEMA: dict[str, tuple[tuple[type, ...], str]] = {
    "pdf_tables": ((bool,), "是否识别 PDF 几何表格"),
    "pdf_ocr": ((bool,), "是否对扫描页做 OCR"),
    "ocr_enabled": ((bool,), "是否启用图片 OCR 解析"),
}


def validate_parse_policy(policy: dict) -> str | None:
    """解析策略入参校验，返回错误信息或 None（与 `validate_chunk_policy` 同风格）。

    未知键也报错：策略键拼错（`pdf_table` 而不是 `pdf_tables`）时，
    静默忽略等于"用户以为自己设了某个开关"——那正是这条校验要防的事。
    """
    if not isinstance(policy, dict):
        return "parse_policy 必须是 JSON 对象"
    for key, value in policy.items():
        spec = PARSE_POLICY_SCHEMA.get(key)
        if spec is None:
            allowed = "、".join(sorted(PARSE_POLICY_SCHEMA))
            return f"未知的解析策略字段 {key!r}（支持：{allowed}）"
        types, _desc = spec
        if bool not in types and isinstance(value, bool):
            return f"{key} 必须是 {types[0].__name__} 类型，收到布尔值"
        if not isinstance(value, types):
            names = "/".join(t.__name__ for t in types)
            return f"{key} 必须是 {names} 类型，收到 {type(value).__name__}"
    return None

# 文件类型展示分组（前端上传向导按此分栏）
TYPE_GROUPS = {
    "document": {
        ".md", ".markdown", ".pdf", ".docx", ".doc", ".xlsx", ".pptx",
        *PLAIN_EXTENSIONS,
    },
    "code": set(CODE_EXTS),
    "image": {".png", ".jpg", ".jpeg"},
}


class ParserRouter:
    def __init__(
        self,
        ocr_parser: OcrParser,
        docx_parser: DocxParser,
        pdf_parser: PdfParser | None = None,
        spreadsheet_parser: SpreadsheetParser | None = None,
        plaintext_parser: PlainTextParser | None = None,
        presentation_parser: PresentationParser | None = None,
    ) -> None:
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

    def supported_extensions(self) -> set[str]:
        """已注册且可路由的扩展名集合（健康检查探针用）。"""
        return set(SUPPORTED_EXTENSIONS)

    def route(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in self._parsers:
            return self._parsers[ext].name
        if ext in TYPE_GROUPS["image"]:
            return "ocr"
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
