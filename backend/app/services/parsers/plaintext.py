"""纯文本类解析器（F3 · 整合 doc-qa 独有功能到 Kquery §2.3）。

覆盖：`.txt/.csv/.json/.yaml/.yml/.html/.htm` 与常见代码文件（`.py/.js/.ts/...`）。

## 三个刻意的取舍

1. **不引 `chardet`**：三级编码回退（utf-8 → gbk → latin-1）能覆盖绝大多数场景，
   少一个依赖就少一处版本冲突与 CI 不稳定因素。
2. **HTML 去标签但不引解析库**：用 `html.parser` 标准库剥标签，
   `<script>` / `<style>` 的**内容**整段丢弃（不是只丢标签）——脚本内容混进
   索引会污染检索语义。
3. **代码文件只做纯文本入索引**：不做符号解析。代码块的价值在"能被搜到"，
   解析符号树的成本与收益不成比例。
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.services.parsers.base import ParseResult
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 纯文本扩展名（按整文件纯文本处理）
TEXT_EXTENSIONS = {
    ".txt", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".log", ".ini", ".toml",
}

#: HTML 扩展名（需要剥标签）
HTML_EXTENSIONS = {".html", ".htm"}

#: 代码文件扩展名（纯文本保留全文）
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".kt", ".swift", ".scala", ".sh", ".bash",
    ".zsh", ".sql", ".r", ".m", ".lua", ".pl", ".vue", ".svelte",
}

#: 编码回退顺序：utf-8 → gbk → latin-1（latin-1 永不失败，作为兜底）
_ENCODINGS = ("utf-8", "gbk", "latin-1")

#: 单文件最大解码字符数（防止超大文本文件一次性进内存）
MAX_TEXT_CHARS = 2_000_000

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


class _TextExtractor(HTMLParser):
    """把 HTML 抽成纯文本（保留块级换行，去掉 script/style 内容）。"""

    _BLOCK_TAGS = {
        "p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
        "section", "article", "header", "footer", "table", "pre", "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._skip_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: D102 - 标准库回调
        if tag in ("script", "style", "noscript", "template"):
            self._skip_depth += 1
            self._skip_tags.append(tag)
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:  # noqa: D102 - 标准库回调
        if self._skip_depth and tag == self._skip_tags[-1]:
            self._skip_depth -= 1
            self._skip_tags.pop()
            return
        if self._skip_depth:
            return
        if tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:  # noqa: D102 - 标准库回调
        if self._skip_depth:
            return
        self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        return re.sub(r"\n{3,}", "\n\n", raw).strip()


def decode_bytes(content: bytes) -> tuple[str, list[str]]:
    """三级编码回退解码。返回 (文本, 警告列表)。"""
    warnings: list[str] = []
    for encoding in _ENCODINGS:
        try:
            return content.decode(encoding), warnings
        except UnicodeDecodeError:
            continue
    # latin-1 不会失败，走到这里说明编码表被改坏了
    return content.decode("utf-8", errors="replace"), ["文本解码异常，已按替换字符处理"]


def html_to_text(raw: str) -> str:
    """HTML → 纯文本：先整段剔除 script/style，再剥标签，最后用解析器兜底。"""
    cleaned = _SCRIPT_STYLE_RE.sub("\n", raw)
    cleaned = _COMMENT_RE.sub(" ", cleaned)
    extractor = _TextExtractor()
    try:
        extractor.feed(cleaned)
        extractor.close()
        return extractor.text()
    except Exception as exc:  # noqa: BLE001 - 畸形 HTML 不该让整个上传失败
        logger.warning("HTML 解析异常，退回正则去标签: %s", exc)
        return re.sub(r"\s+", " ", _TAG_RE.sub(" ", cleaned)).strip()


def _pretty_json(raw: str) -> str:
    """JSON 尽量格式化（便于人读与切块），失败则原样返回。"""
    try:
        return json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001 - 非法 JSON 按纯文本处理
        return raw


class PlainTextParser:
    """纯文本 / 代码 / HTML 解析器。"""

    name = "plaintext"

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        ext = path.suffix.lower()
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ApiError(ErrCode.PARSE_FAILED, f"文件读取失败: {exc}") from exc

        text, warnings = decode_bytes(content)
        if len(text) > MAX_TEXT_CHARS:
            warnings.append(f"文件超过 {MAX_TEXT_CHARS} 字符，已截断")
            text = text[:MAX_TEXT_CHARS]

        if ext in HTML_EXTENSIONS:
            text = html_to_text(text)
        elif ext in (".json", ".jsonl"):
            text = _pretty_json(text)

        if not text.strip():
            raise ApiError(ErrCode.PARSE_FAILED, "文档内容为空")

        source = {
            ".txt": "全文", ".csv": "表格数据", ".tsv": "表格数据", ".json": "全文",
            ".jsonl": "全文", ".yaml": "全文", ".yml": "全文", ".log": "全文",
            ".ini": "全文", ".toml": "全文", ".html": "正文", ".htm": "正文",
        }.get(ext, "代码")
        return ParseResult(segments=[(source, text)], warnings=warnings)
