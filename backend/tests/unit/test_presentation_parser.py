"""PPT/PPTX 解析器单测（F9 · 补齐 doc-qa 的演示文稿能力）。"""
from pathlib import Path

import pytest

from app.core.envelope import ApiError
from app.services.parsers.presentation import PresentationParser


def _make_pptx(path: Path, slides: list[dict]) -> Path:
    """造一个最小可解析的 pptx。

    slides 元素：{"title": str, "body": str, "table": list[list[str]], "notes": str}
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    layout = prs.slide_layouts[5]  # 空白版式
    for spec in slides:
        slide = prs.slides.add_slide(layout)
        if spec.get("title"):
            box = slide.shapes.add_textbox(
                Inches(1), Inches(1), Inches(6), Inches(1)
            )
            box.text_frame.text = spec["title"]
        if spec.get("body"):
            box = slide.shapes.add_textbox(
                Inches(1), Inches(2), Inches(6), Inches(2)
            )
            box.text_frame.text = spec["body"]
        if spec.get("table"):
            rows = spec["table"]
            shape = slide.shapes.add_table(
                len(rows), len(rows[0]), Inches(1), Inches(4), Inches(6), Inches(2)
            )
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    shape.table.cell(r, c).text = value
        if spec.get("notes"):
            slide.notes_slide.notes_text_frame.text = spec["notes"]
    prs.save(str(path))
    return path


class TestPresentationParser:
    def test_parses_slides(self, tmp_path):
        path = _make_pptx(
            tmp_path / "deck.pptx",
            [
                {"title": "第一页标题", "body": "第一页正文"},
                {"title": "第二页标题", "body": "第二页正文"},
            ],
        )
        result = PresentationParser().parse(path, "doc1", "t1", {})
        assert len(result.segments) == 2
        assert "第一页标题" in result.segments[0][1]
        assert "第二页正文" in result.segments[1][1]
        assert result.segments[0][0] == "第 1 页"
        assert result.segments[1][0] == "第 2 页"

    def test_extracts_table(self, tmp_path):
        path = _make_pptx(
            tmp_path / "deck.pptx",
            [{"title": "报价", "table": [["项目", "价格"], ["A", "10"]]}],
        )
        result = PresentationParser().parse(path, "doc1", "t1", {})
        assert len(result.tables) == 1
        assert result.tables[0].rows == 2
        assert result.tables[0].cols == 2
        assert "项目" in result.tables[0].markdown
        # 表格单独成段，且 source 带「·表格」标记：
        # apply_chunking 靠它把表格段单独成块（modality=table）
        table_segments = [s for s, _ in result.segments if "·表格" in s]
        assert len(table_segments) == 1, result.segments
        assert "项目" in dict(result.segments)[table_segments[0]]

    def test_extracts_notes(self, tmp_path):
        path = _make_pptx(
            tmp_path / "deck.pptx",
            [{"title": "口径", "notes": "这是内部口径，不对外说"}],
        )
        result = PresentationParser().parse(path, "doc1", "t1", {})
        assert "[备注] 这是内部口径，不对外说" in result.segments[0][1]

    def test_empty_deck_returns_empty(self, tmp_path):
        from pptx import Presentation

        path = tmp_path / "empty.pptx"
        Presentation().save(str(path))
        result = PresentationParser().parse(path, "doc1", "t1", {})
        assert result.segments == []
        assert any("为空" in w for w in result.warnings)

    def test_corrupt_file_raises_readable_error(self, tmp_path):
        path = tmp_path / "broken.pptx"
        path.write_bytes(b"not a pptx at all")
        with pytest.raises(ApiError) as exc:
            PresentationParser().parse(path, "doc1", "t1", {})
        assert exc.value.code == 3201
        assert "无法打开" in exc.value.message

    def test_router_routes_pptx(self, tmp_path):
        from app.services.parsers.router import SUPPORTED_EXTENSIONS

        assert ".pptx" in SUPPORTED_EXTENSIONS
