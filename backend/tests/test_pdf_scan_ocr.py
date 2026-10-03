"""扫描版 PDF 自动 OCR 测试（按页混合策略）。

测试数据用 pymupdf 现场构造：
- 文字页：insert_text 写入正文（有文本层）；
- 扫描页：仅插入位图（无文本层），模拟扫描件；
OCR 引擎用确定性假实现，验证路由/表格剥离/资源落盘/护栏逻辑，
真实 qwen-vl-ocr 的质量由端到端冒烟覆盖。
"""
from pathlib import Path

import pymupdf
import pytest

from app.core.envelope import ApiError
from app.services.parsers.pdf import PdfParser


class FakeOcrEngine:
    """确定性 OCR：返回含 Markdown 表格的固定文本。"""

    def __init__(self):
        self.calls: list[str] = []

    def recognize(self, image_path: Path) -> str:
        self.calls.append(image_path.name)
        return (
            "第2章 资产类别与金融工具\n\n"
            "货币市场工具是期限短、流动性好的债务证券。\n\n"
            "| 工具 | 期限 | 风险 |\n"
            "| --- | --- | --- |\n"
            "| 短期国库券 | 一年以内 | 极低 |\n"
            "| 商业票据 | 270天以内 | 低 |\n"
        )


def make_text_pdf(path: Path, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"这是第{i+1}页的正文内容，" * 10)
    doc.save(str(path))
    doc.close()
    return path


def _make_image_page(doc) -> None:
    """插入一页纯位图（无文本层），模拟扫描页。"""
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 200, 100))
    pix.clear_with(230)  # 灰底
    page.insert_image(page.rect, pixmap=pix)


def make_scanned_pdf(path: Path, pages: int = 2) -> Path:
    doc = pymupdf.open()
    for _ in range(pages):
        _make_image_page(doc)
    doc.save(str(path))
    doc.close()
    return path


def make_mixed_pdf(path: Path) -> Path:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "这是第1页的正文内容，" * 10)
    _make_image_page(doc)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def ocr_engine():
    return FakeOcrEngine()


@pytest.fixture()
def parser(tmp_path, ocr_engine):
    return PdfParser(ocr_engine=ocr_engine, assets_dir=tmp_path / "assets")


class TestTextLayerPdf:
    def test_text_pdf_never_calls_ocr(self, tmp_path, parser, ocr_engine):
        path = make_text_pdf(tmp_path / "text.pdf")
        result = parser.parse(path, "doc-t", "tenant", {})
        assert ocr_engine.calls == []
        assert any("第1页" in src for src, _ in result.segments)
        assert not any("扫描" in w for w in result.warnings)


class TestScannedPdf:
    def test_scanned_pages_go_to_ocr(self, tmp_path, parser, ocr_engine):
        path = make_scanned_pdf(tmp_path / "scan.pdf")
        result = parser.parse(path, "doc-s", "tenant", {})
        assert len(ocr_engine.calls) == 2  # 两页都走了 OCR
        assert any("第1页·OCR" in src for src, _ in result.segments)
        assert any("第2页·OCR" in src for src, _ in result.segments)
        assert any("扫描页" in w and "OCR" in w for w in result.warnings)

    def test_ocr_tables_extracted_as_records(self, tmp_path, parser):
        """OCR 文本中的 Markdown 表格被剥离为 TableRecord（结构保护）。"""
        path = make_scanned_pdf(tmp_path / "scan.pdf", pages=1)
        result = parser.parse(path, "doc-s", "tenant", {})
        assert len(result.tables) == 1
        table = result.tables[0]
        assert table.table_id == "doc-s#tbl-1-ocr1"
        assert "| 短期国库券 |" in table.markdown
        # 表格段以独立片段进入切块（带表格标记位由入库层识别）
        assert any("表格" in src for src, _ in result.segments)
        # 正文段不含表格内容（已剥离）
        body = next(text for src, text in result.segments if "·OCR" in src)
        assert "短期国库券" not in body
        assert "货币市场工具" in body

    def test_page_images_saved_as_assets(self, tmp_path, parser):
        path = make_scanned_pdf(tmp_path / "scan.pdf", pages=1)
        result = parser.parse(path, "doc-s", "tenant", {})
        assert len(result.assets) == 1
        asset = result.assets[0]
        assert asset.kind == "page_image"
        assert asset.page == 1
        assert Path(asset.uri).exists()

    def test_scanned_pdf_without_ocr_engine_fails(self, tmp_path):
        path = make_scanned_pdf(tmp_path / "scan.pdf")
        parser = PdfParser(ocr_engine=None)
        with pytest.raises(ApiError, match="OCR 不可用"):
            parser.parse(path, "doc-s", "tenant", {})

    def test_ocr_disabled_by_policy(self, tmp_path, parser, ocr_engine):
        path = make_scanned_pdf(tmp_path / "scan.pdf")
        with pytest.raises(ApiError):
            parser.parse(path, "doc-s", "tenant", {"pdf_ocr": False})
        assert ocr_engine.calls == []

    def test_max_pages_cap(self, tmp_path, ocr_engine):
        path = make_scanned_pdf(tmp_path / "scan.pdf", pages=3)
        parser = PdfParser(ocr_engine=ocr_engine, ocr_max_pages=2)
        result = parser.parse(path, "doc-s", "tenant", {})
        assert len(ocr_engine.calls) == 2  # 第 3 页被护栏拦截
        assert any("页数上限" in w for w in result.warnings)


class TestMixedPdf:
    def test_mixed_hybrid_strategy(self, tmp_path, parser, ocr_engine):
        path = make_mixed_pdf(tmp_path / "mixed.pdf")
        result = parser.parse(path, "doc-m", "tenant", {})
        # 第 1 页文字流，第 2 页 OCR——按页混合
        assert len(ocr_engine.calls) == 1
        sources = [src for src, _ in result.segments]
        assert any(src == "第1页" for src in sources)
        assert any("第2页·OCR" in src for src in sources)
