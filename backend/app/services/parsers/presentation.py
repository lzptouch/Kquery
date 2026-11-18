"""演示文稿解析器（F9 · 补齐 doc-qa 的 PPT 能力）：`.pptx` → 逐页文本 + 表格记录。

## 为什么用 `python-pptx` 而不是移植 doc-qa 的实现

doc-qa 的 `deepdoc/parser/ppt_parser.py` 只有 68 行，逻辑就是
"遍历幻灯片 → 抽文本框 → 抽表格"，依赖同样是 `python-pptx`。
它之所以值得单独一个模块，是因为 DeepDOC 那套 OCR/版面分析栈太重，
**幻灯片本身是结构化 XML，根本不需要 OCR**——直接读 OOXML 更准更快。

## 产出

- 每页一个文本段（标题 + 正文 + 表格），`source` 形如 `第 3 页`；
- 页内表格额外产出一条 `TableRecord`（单独成块，modality=table）；
- 备注页（speaker notes）并入该页文本——演示稿里最常被问到的
  往往是备注里的口径，而不是幻灯片上的标题。

## 大文件保护

单页形状数上限 + 总页数上限 + 单段字符上限：PPT 被做成"一页十万个文本框"
并不罕见，全量展开会把内存吃光。
"""
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import TableRecord
from app.services.parsers.base import ParseResult
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 单页最多抽取的形状数（超出截断）
MAX_SHAPES_PER_SLIDE = 500
#: 最多解析的幻灯片数
MAX_SLIDES = 500
#: 单段最大字符数
MAX_SEGMENT_CHARS = 20_000
#: 单个单元格最大字符数
MAX_CELL_CHARS = 2_000


def _cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) > MAX_CELL_CHARS:
        text = text[:MAX_CELL_CHARS] + "…"
    return text.replace("\n", " ").replace("|", "\\|").strip()


def _to_markdown(rows: list[list[str]]) -> str:
    """二维数组 → Markdown 表格（首行作表头）。"""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(padded[0]) + " |"]
    lines.append("| " + " | ".join(["---"] * width) + " |")
    for row in padded[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _shape_text(shape) -> str:
    """取一个形状的纯文本（文本框 / 占位符 / 组合内的子形状）。"""
    texts: list[str] = []

    if getattr(shape, "has_text_frame", False):
        for para in shape.text_frame.paragraphs:
            line = "".join(run.text for run in para.runs).strip()
            if not line:
                # runs 为空但 paragraph 有文字的情况（文本被拆分到别的结构里）
                line = (para.text or "").strip()
            if line:
                texts.append(line)

    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            texts.append(
                " | ".join(_cell(cell.text) for cell in row.cells)
            )

    # 组合形状（SmartArt 常常是组合）
    if getattr(shape, "shape_type", None) is not None and hasattr(shape, "shapes"):
        try:
            for sub in shape.shapes:
                sub_text = _shape_text(sub)
                if sub_text:
                    texts.append(sub_text)
        except (AttributeError, TypeError):
            pass

    return "\n".join(texts)


def _table_rows(shape) -> list[list[str]]:
    if not getattr(shape, "has_table", False):
        return []
    return [[_cell(cell.text) for cell in row.cells] for row in shape.table.rows]


class PresentationParser:
    """`.pptx` 解析器：每页产出一个文本段，页内表格额外产出一条表格记录。"""

    name = "presentation"

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        try:
            from pptx import Presentation
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
            raise ApiError(
                ErrCode.PARSE_FAILED, "服务端缺少 python-pptx，无法解析 .pptx"
            ) from exc

        warnings: list[str] = []
        segments: list[tuple[str, str]] = []
        tables: list[TableRecord] = []

        try:
            prs = Presentation(str(path))
        except Exception as exc:  # noqa: BLE001 - 损坏/加密文件给可读错误
            raise ApiError(
                ErrCode.PARSE_FAILED, f"演示文稿无法打开（可能已损坏或加密）: {exc}"
            ) from exc

        slides = list(prs.slides)
        if len(slides) > MAX_SLIDES:
            warnings.append(f"幻灯片超过 {MAX_SLIDES} 页，仅解析前 {MAX_SLIDES} 页")
            slides = slides[:MAX_SLIDES]

        for idx, slide in enumerate(slides, start=1):
            chunks: list[str] = []
            slide_tables: list[list[list[str]]] = []

            shapes = list(slide.shapes)
            if len(shapes) > MAX_SHAPES_PER_SLIDE:
                warnings.append(f"第 {idx} 页形状超过 {MAX_SHAPES_PER_SLIDE} 个，已截断")
                shapes = shapes[:MAX_SHAPES_PER_SLIDE]

            for shape in shapes:
                text = _shape_text(shape)
                if text.strip():
                    chunks.append(text.strip())
                rows = _table_rows(shape)
                if rows:
                    slide_tables.append(rows)

            # 备注页：演示稿里最常被问到的口径常写在这里
            try:
                notes = slide.notes_slide.notes_text_frame.text
            except Exception:  # noqa: BLE001 - 没有备注页/备注为空都属正常
                notes = ""
            if notes and notes.strip():
                chunks.append(f"[备注] {notes.strip()}")

            # 表格单独成段：source 带「·表格」标记，
            # knowledge.apply_chunking 靠它把表格段单独成块（modality=table），
            # 不被切块器拦腰切断。
            for t_idx, rows in enumerate(slide_tables):
                markdown = _to_markdown(rows)
                if not markdown.strip():
                    continue
                tables.append(
                    TableRecord(
                        table_id=f"{doc_id}-t{idx}-{t_idx}",
                        doc_id=doc_id,
                        page=idx - 1,
                        table_index=len(tables),
                        rows=len(rows),
                        cols=max(len(r) for r in rows),
                        markdown=markdown,
                    )
                )
                segments.append((f"·表格:第 {idx} 页表格 {t_idx + 1}", markdown))

            if not chunks:
                continue

            body = "\n\n".join(chunks)
            if len(body) > MAX_SEGMENT_CHARS:
                body = body[:MAX_SEGMENT_CHARS] + "…"
                warnings.append(f"第 {idx} 页内容过长，已截断")

            segments.append((f"第 {idx} 页", body))

        if not segments:
            warnings.append("演示文稿为空，未解析到任何内容")
            return ParseResult(segments=[], tables=[], warnings=warnings)

        return ParseResult(segments=segments, tables=tables, warnings=warnings)
