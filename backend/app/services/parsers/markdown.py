"""Markdown 解析器：全文 + 章节大纲。"""
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.services.parsers.base import ParseResult, extract_md_outline


class MarkdownParser:
    name = "markdown"

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "Markdown 文件不是有效的 UTF-8 编码") from exc
        if not text.strip():
            raise ApiError(ErrCode.DOCUMENT_PARSE_FAILED, "文档内容为空")
        return ParseResult(
            segments=[("全文", text)],
            outline=extract_md_outline(text),
        )
