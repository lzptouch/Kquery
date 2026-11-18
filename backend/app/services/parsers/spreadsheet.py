"""表格解析器（F3 · 整合 doc-qa 独有功能到 Kquery §2.3）：`.xlsx` → 文本段 + 表格记录。

## 为什么只支持 `.xlsx`

openpyxl 读不了 BIFF 格式的 `.xls`。doc-qa 的正则 `\\.xlsx?$` 写了 `.xls`，
但落到 openpyxl 照样失败——它并没有真正支持 `.xls`，只是错误暴露得晚。
要真支持得引长期无维护的 `xlrd<2.0`，不做。`.xls` 在入口直接给可读错误。

## 大文件保护

`read_only=True` 流式读行 + 单表行数上限 + 总段数上限：Excel 被人做成
"一个 sheet 一百万行"是常见的事，一次性全量展开会把内存吃光。
"""
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import TableRecord
from app.services.parsers.base import ParseResult
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 单个 sheet 最大行数（超出截断并写 warning）
MAX_SHEET_ROWS = 20_000
#: 单个工作簿最大展开段数
MAX_SHEETS = 200
#: 单格最大字符数（防止一个巨型单元格撑爆上下文）
MAX_CELL_CHARS = 4_000


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


class SpreadsheetParser:
    """`.xlsx` 解析器：每个 sheet 产出一个文本段与一条表格记录。"""

    name = "spreadsheet"

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的兜底
            raise ApiError(
                ErrCode.PARSE_FAILED, "服务端缺少 openpyxl，无法解析 .xlsx"
            ) from exc

        warnings: list[str] = []
        segments: list[tuple[str, str]] = []
        tables: list[TableRecord] = []

        try:
            wb = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 - 损坏/加密文件给可读错误
            raise ApiError(
                ErrCode.PARSE_FAILED, f"Excel 文件无法打开（可能已损坏或加密）: {exc}"
            ) from exc

        try:
            sheets = list(wb.worksheets)[:MAX_SHEETS]
            if len(wb.worksheets) > MAX_SHEETS:
                warnings.append(f"工作表超过 {MAX_SHEETS} 个，仅解析前 {MAX_SHEETS} 个")

            for idx, sheet in enumerate(sheets):
                rows: list[list[str]] = []
                truncated = False
                for raw_row in sheet.iter_rows(values_only=True):
                    if len(rows) >= MAX_SHEET_ROWS:
                        truncated = True
                        break
                    rows.append([_cell(c) for c in raw_row])
                if truncated:
                    warnings.append(
                        f"工作表「{sheet.title}」超过 {MAX_SHEET_ROWS} 行，已截断"
                    )
                if not rows:
                    continue
                markdown = _to_markdown(rows)
                if not markdown.strip():
                    continue
                tables.append(
                    TableRecord(
                        table_id=f"{doc_id}-t{idx}",
                        doc_id=doc_id,
                        page=idx,
                        table_index=idx,
                        rows=len(rows),
                        cols=max(len(r) for r in rows),
                        markdown=markdown,
                    )
                )
                # source 必须带「·表格」标记：knowledge.apply_chunking 靠它把表格段
                # 单独成块（modality=table），不被切块器拦腰切断。
                segments.append((f"·表格:{sheet.title}", markdown))
        finally:
            try:
                wb.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响已解析结果
                pass

        if not segments:
            # 空工作簿不是错误，但必须让上层知道"解析出来是空的"
            warnings.append("工作簿为空，未解析到任何数据")
            return ParseResult(segments=[], tables=[], warnings=warnings)

        return ParseResult(segments=segments, tables=tables, warnings=warnings)
