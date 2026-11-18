"""F3 表格解析器单测（.xlsx）。"""
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.core.envelope import ApiError, ErrCode
from app.services.parsers.spreadsheet import SpreadsheetParser


def _write_xlsx(path: Path, sheets: dict[str, list[list[str]]]) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


class TestSpreadsheetParser:
    def test_parses_sheets_and_tables(self, tmp_path: Path):
        path = _write_xlsx(
            tmp_path / "data.xlsx",
            {
                "销售": [["城市", "金额"], ["北京", "100"], ["上海", "200"]],
                "库存": [["sku", "数量"], ["A-1", "5"]],
            },
        )
        result = SpreadsheetParser().parse(path, "doc1", "t1", {})

        assert len(result.segments) == 2, "两个 sheet 应产出两个文本段"
        assert len(result.tables) == 2, "两个 sheet 应产出两条表格记录"
        table = result.tables[0]
        assert table.rows == 3 and table.cols == 2
        assert table.doc_id == "doc1"
        assert "城市" in table.markdown and "北京" in table.markdown
        # 表格段必须带「·表格」标记：knowledge.apply_chunking 靠它单独成块
        assert "·表格" in result.segments[0][0]

    def test_empty_workbook_warns(self, tmp_path: Path):
        # openpyxl 不允许保存"零个可见工作表"的工作簿，这里用"有一个空表"等价表达
        path = _write_xlsx(tmp_path / "empty.xlsx", {"空表": []})
        result = SpreadsheetParser().parse(path, "doc1", "t1", {})
        assert result.segments == []
        assert result.warnings, "空工作簿必须给出 warning，而不是静默返回空"

    def test_row_limit_truncates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        import app.services.parsers.spreadsheet as mod

        monkeypatch.setattr(mod, "MAX_SHEET_ROWS", 3)
        path = _write_xlsx(
            tmp_path / "big.xlsx",
            {"S": [["h"]] + [[str(i)] for i in range(10)]},
        )
        result = SpreadsheetParser().parse(path, "doc1", "t1", {})
        assert result.tables[0].rows == 3, "超限行数必须被截断"
        assert any("截断" in w for w in result.warnings)
        assert mod.MAX_SHEET_ROWS == 3  # monkeypatch 生效确认

    def test_broken_file_raises_readable_error(self, tmp_path: Path):
        path = tmp_path / "broken.xlsx"
        path.write_bytes(b"not a real xlsx")
        with pytest.raises(ApiError) as exc:
            SpreadsheetParser().parse(path, "doc1", "t1", {})
        assert exc.value.code == ErrCode.PARSE_FAILED
        assert "损坏" in exc.value.message or "无法打开" in exc.value.message
