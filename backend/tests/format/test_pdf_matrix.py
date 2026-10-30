"""PDF 专项（矩阵 §三，30 例 / 13 类样本）—— **第一门禁**。

为什么 PDF 单独占一个门禁：PDF 是被测格式里**唯一有"同一页可能既是文字页
又是扫描页"**这种混合情况的类型。整篇一刀切（全部走 OCR 或全部走文本流）
会让文字页内容丢失、或让扫描页拿不到文字——两种都是用户可见的严重缺陷。
所以 PDF 的 P0 用例（19/30）几乎都围绕"逐页独立判定"这一件事。

编号与矩阵 §3.4 / §3.5 一一对应，便于回溯：
- PDF-01~PDF-19、PDF-23~PDF-26：解析器层（本文件）
- PDF-20~PDF-22、PDF-27~PDF-30：端到端 / API 层（`test_format_e2e.py`）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.envelope import ApiError
from app.services.parsers.pdf import PdfParser

from .contract import assert_idempotent, chunk_like_production
from .fixtures import (
    make_corrupt_pdf,
    make_empty_pdf,
    make_encrypted_pdf,
    make_mixed_pdf,
    make_outline_pdf,
    make_pdf_with_embedded_image,
    make_scanned_pdf,
    make_table_pdf,
    make_text_pdf,
    make_two_column_pdf,
)

DOC_ID = "pdf-doc"


@pytest.fixture
def write_pdf(tmp_path: Path):
    def _write(data: bytes, name: str = "sample.pdf") -> Path:
        path = tmp_path / name
        path.write_bytes(data)
        return path

    return _write


# --------------------------------------------------------------------------
# S1/S2 文字页
# --------------------------------------------------------------------------


def test_pdf01_text_page_extraction(write_pdf, ocr_engine) -> None:
    """PDF-01：文字页抽取 —— 1 段、source=第1页、**不调 OCR**、无「扫描」warning。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_text_pdf(pages=1)), DOC_ID, "default", {})

    assert len(result.segments) == 1, f"期望 1 段，实际 {len(result.segments)}"
    source, text = result.segments[0]
    assert source == "第1页", f"source 应为「第1页」，实际 {source!r}"
    assert text.strip(), "文字页文本为空"
    assert ocr_engine.calls == [], f"文字页不应触发 OCR，实际调用 {len(ocr_engine.calls)} 次"
    assert not any("扫描" in w for w in result.warnings), (
        f"文字页不应有「扫描」warning：{result.warnings}"
    )


def test_pdf02_multipage_not_merged(write_pdf, ocr_engine) -> None:
    """PDF-02：多页**不跨页合并** —— 3 页 → 3 段，source 逐页。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_text_pdf(pages=3)), DOC_ID, "default", {})

    assert len(result.segments) == 3, f"期望 3 段，实际 {len(result.segments)}"
    sources = [s for s, _ in result.segments]
    assert sources == ["第1页", "第2页", "第3页"], f"source 序列异常：{sources}"
    assert ocr_engine.calls == []


def test_pdf19_idempotent(write_pdf, ocr_engine) -> None:
    """PDF-19：同一文件解析两次 → 产物一致。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    path = write_pdf(make_text_pdf(pages=2))
    assert_idempotent(lambda: parser.parse(path, DOC_ID, "default", {}))


# --------------------------------------------------------------------------
# S3/S4/S5 扫描页与混合
# --------------------------------------------------------------------------


def test_pdf03_scanned_page_uses_ocr(write_pdf, ocr_engine) -> None:
    """PDF-03：扫描页走 OCR —— source=第1页·OCR、OCR 调用 1 次、warning 含扫描页与 OCR。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_scanned_pdf(pages=1)), DOC_ID, "default", {})

    sources = [s for s, _ in result.segments]
    assert "第1页·OCR" in sources, f"缺少 OCR 段：{sources}"
    assert len(ocr_engine.calls) == 1, f"OCR 应调用 1 次，实际 {len(ocr_engine.calls)}"
    joined = " ".join(result.warnings)
    assert "扫描页" in joined, f"warning 应含「扫描页」：{result.warnings}"
    assert "OCR" in joined, f"warning 应含「OCR」：{result.warnings}"


def test_pdf04_multipage_scanned_each_page(write_pdf, ocr_engine) -> None:
    """PDF-04：多页扫描 → 每页独立判定、每页独立 OCR。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_scanned_pdf(pages=3)), DOC_ID, "default", {})

    assert len(ocr_engine.calls) == 3, f"OCR 应调用 3 次，实际 {len(ocr_engine.calls)}"
    sources = [s for s, _ in result.segments]
    for n in (1, 2, 3):
        assert f"第{n}页·OCR" in sources, f"缺少第{n}页的 OCR 段：{sources}"


def test_pdf05_mixed_pdf_page_by_page(write_pdf, ocr_engine) -> None:
    """PDF-05：**混合逐页判定** —— 文字页 + 扫描页，只有扫描页走 OCR。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_mixed_pdf()), DOC_ID, "default", {})

    assert len(ocr_engine.calls) == 1, (
        f"混合文档只应对扫描页调 1 次 OCR，实际 {len(ocr_engine.calls)} 次"
    )
    sources = [s for s, _ in result.segments]
    assert "第1页" in sources, f"缺少文字页段：{sources}"
    assert "第2页·OCR" in sources, f"缺少扫描页 OCR 段：{sources}"


def test_pdf14_ocr_engine_unavailable(write_pdf, assets_dir) -> None:
    """PDF-14：无 OCR 引擎 → 扫描页明确报错（**不静默产出空索引**）。"""
    parser = PdfParser(ocr_engine=None, assets_dir=assets_dir)
    with pytest.raises(ApiError) as exc:
        parser.parse(write_pdf(make_scanned_pdf(pages=1)), DOC_ID, "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert "OCR 不可用" in exc.value.message, (
        f"错误文案应说明 OCR 不可用：{exc.value.message}"
    )


def test_pdf15_pdf_ocr_policy_off(write_pdf, ocr_engine) -> None:
    """PDF-15：`pdf_ocr=False` → 扫描页不入库，整篇无文本 → 3003。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    with pytest.raises(ApiError) as exc:
        parser.parse(
            write_pdf(make_scanned_pdf(pages=1)), DOC_ID, "default", {"pdf_ocr": False}
        )
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert ocr_engine.calls == [], f"策略关闭后不应调 OCR，实际 {len(ocr_engine.calls)} 次"


def test_pdf13_ocr_page_limit(write_pdf, ocr_engine, assets_dir) -> None:
    """PDF-13：OCR 页数上限 —— 超限页**不入库**且有 warning。"""
    parser = PdfParser(ocr_engine=ocr_engine, assets_dir=assets_dir, ocr_max_pages=2)
    result = parser.parse(write_pdf(make_scanned_pdf(pages=3)), DOC_ID, "default", {})

    assert len(ocr_engine.calls) == 2, (
        f"OCR 应被限到 2 次，实际 {len(ocr_engine.calls)} 次"
    )
    joined = " ".join(result.warnings)
    assert "页数上限" in joined, f"warning 应含「页数上限」：{result.warnings}"
    sources = [s for s, _ in result.segments]
    assert "第3页·OCR" not in sources, f"第 3 页不应入库：{sources}"


def test_pdf17_ocr_table_stripping(write_pdf, assets_dir) -> None:
    """PDF-17：OCR 文本里的 Markdown 表格被剥离为独立 TableRecord。

    这条最关键：表格混在正文里会被切块器拦腰切断，
    单元格与表头的对应关系一旦断掉，检索结果就是错的。

    回归：清洗逻辑曾把表格的**分隔行**当噪声删掉，导致
    3 行的表（表头+分隔+1 数据行）根本匹配不上表识别正则。
    """
    from .conftest import CountingOcrEngine

    engine = CountingOcrEngine(
        text=(
            "这是扫描件正文，特征串 OCRBODY。\n\n"
            "| 工具 | 期限 | 风险 |\n"
            "| --- | --- | --- |\n"
            "| 短期国库券 | 一年以内 | 极低 |\n"
        )
    )
    parser = PdfParser(ocr_engine=engine, assets_dir=assets_dir)
    result = parser.parse(write_pdf(make_scanned_pdf(pages=1)), DOC_ID, "default", {})

    assert result.tables, "OCR 表格未被剥离成 TableRecord"
    table = result.tables[0]
    assert table.table_id == f"{DOC_ID}#tbl-1-ocr1", f"table_id 异常：{table.table_id}"
    assert table.rows >= 2 and table.cols >= 2, f"表格行列异常：{table.rows}x{table.cols}"
    # 分隔行必须保留：缺了它，markdown 就不是合法表格，前端渲染不出来
    assert "---" in table.markdown, (
        f"表格 markdown 丢了分隔行，不是合法 Markdown 表格：{table.markdown!r}"
    )
    body = " ".join(text for source, text in result.segments if "·表格" not in source)
    assert "| 工具 |" not in body, "表格行混进了正文段"
    assert "OCRBODY" in body, f"正文本身丢了：{body!r}"


def test_pdf18_page_bitmap_asset(write_pdf, ocr_engine, assets_dir) -> None:
    """PDF-18：扫描页位图存档 —— kind=page_image、文件真实存在、hash 非空。"""
    parser = PdfParser(ocr_engine=ocr_engine, assets_dir=assets_dir)
    result = parser.parse(write_pdf(make_scanned_pdf(pages=1)), DOC_ID, "default", {})

    assert result.assets, "缺少页面位图资源"
    asset = result.assets[0]
    assert asset.kind == "page_image", f"kind 应为 page_image，实际 {asset.kind!r}"
    assert asset.page == 1, f"page 应为 1，实际 {asset.page}"
    assert Path(asset.uri).exists(), f"位图文件不存在：{asset.uri}"
    assert asset.hash, "hash 为空"


# --------------------------------------------------------------------------
# S6/S7/S8 失败路径
# --------------------------------------------------------------------------


def test_pdf06_encrypted_pdf(write_pdf, ocr_engine) -> None:
    """PDF-06：加密 PDF → 3003 且文案含「已加密」，**不产出空索引**。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    with pytest.raises(ApiError) as exc:
        parser.parse(write_pdf(make_encrypted_pdf()), DOC_ID, "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert "已加密" in exc.value.message, f"文案应含「已加密」：{exc.value.message}"


def test_pdf07_corrupt_pdf(write_pdf, ocr_engine) -> None:
    """PDF-07：损坏 PDF → 3003 且文案含「打开失败」。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    with pytest.raises(ApiError) as exc:
        parser.parse(write_pdf(make_corrupt_pdf()), DOC_ID, "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert "打开失败" in exc.value.message, f"文案应含「打开失败」：{exc.value.message}"


def test_pdf08_no_text_at_all(write_pdf, assets_dir) -> None:
    """PDF-08：全篇无文本 → 3003 且文案含「未抽取到文本」。

    这里让 OCR **识别不出任何文字**（而不是没有 OCR 引擎）：
    没有引擎时的报错文案是「OCR 不可用」，那是 PDF-14 覆盖的路径；
    本条要验的是"整篇真的没有内容"这条分支——两条路径的文案不同，不能混。
    """
    from .conftest import CountingOcrEngine

    engine = CountingOcrEngine(text="")
    parser = PdfParser(ocr_engine=engine, assets_dir=assets_dir)
    with pytest.raises(ApiError) as exc:
        parser.parse(write_pdf(make_empty_pdf()), DOC_ID, "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert "未抽取到文本" in exc.value.message, (
        f"文案应含「未抽取到文本」：{exc.value.message}"
    )


# --------------------------------------------------------------------------
# S9 几何表格
# --------------------------------------------------------------------------


def test_pdf09_geometric_table(write_pdf, ocr_engine) -> None:
    """PDF-09：几何表格识别 —— table_id 命名、行列数、markdown 含表头。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_table_pdf(rows=3, cols=3)), DOC_ID, "default", {})

    assert result.tables, "未识别到几何表格"
    table = result.tables[0]
    assert table.table_id == f"{DOC_ID}#tbl-1-1", f"table_id 异常：{table.table_id}"
    assert table.rows >= 2 and table.cols >= 2, f"行列数异常：{table.rows}x{table.cols}"
    assert "R0C0" in table.markdown, f"markdown 应含表头：{table.markdown[:120]!r}"


def test_pdf10_table_becomes_own_chunk(write_pdf, ocr_engine) -> None:
    """PDF-10：表格**单独成块**（modality=table），正文段不含表格 markdown。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_table_pdf(rows=3, cols=3)), DOC_ID, "default", {})

    chunks = chunk_like_production(DOC_ID, result)
    table_chunks = [c for c in chunks if c.modality == "table"]
    assert table_chunks, "切块后没有 modality=='table' 的块——表格会被切碎"

    markdown = result.tables[0].markdown.strip()
    first_line = markdown.splitlines()[0].strip()
    for source, text in result.segments:
        if "·表格" in source:
            continue
        assert first_line not in text, f"正文段混进了表格表头：{source!r}"


def test_pdf16_pdf_tables_policy_off(write_pdf, ocr_engine) -> None:
    """PDF-16：`pdf_tables=False` → 不做几何表格识别（表格文字留在正文）。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(
        write_pdf(make_table_pdf(rows=3, cols=3)), DOC_ID, "default", {"pdf_tables": False}
    )
    assert result.tables == [], f"策略关闭后不应有表格：{result.tables}"


# --------------------------------------------------------------------------
# S10/S12 大纲
# --------------------------------------------------------------------------


def test_pdf11_outline_extraction(write_pdf, ocr_engine) -> None:
    """PDF-11：字号法大纲 —— 含「第一章」、level ∈ {1,2,3}、标题长度 ≤60。"""
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_outline_pdf()), DOC_ID, "default", {})

    assert result.outline, "未抽取出大纲"
    titles = [item["title"] for item in result.outline]
    assert any("第一章" in t for t in titles), f"大纲缺少「第一章」：{titles}"
    for item in result.outline:
        assert item["level"] in (1, 2, 3), f"level 越界：{item}"
        assert len(item["title"]) <= 60, f"标题超长（>60 字，应被排除）：{item}"


def test_pdf12_outline_sample_too_small(write_pdf, ocr_engine) -> None:
    """PDF-12：大纲样本不足（<3 行）→ outline 为空（**设计如此**，非缺陷）。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Title", fontsize=20)
    page.insert_text((72, 140), "Body", fontsize=10)
    buf = doc.tobytes()
    doc.close()

    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(buf), DOC_ID, "default", {})
    assert result.outline == [], f"样本不足时大纲应为空，实际 {result.outline}"


# --------------------------------------------------------------------------
# S13 双栏 / 内嵌图 / 白名单（现状登记）
# --------------------------------------------------------------------------


def test_pdf23_two_column_current_behaviour(write_pdf, ocr_engine) -> None:
    """PDF-23：双栏 —— **登记当前行为**（顺序可能混杂），不断言"必须正确"。

    矩阵 §1.4 已把"双栏只缓解不根治"登记为已知限制。
    这条用例的价值是：**行为变化时能第一时间发现**，从而去评估要不要修。
    """
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(write_pdf(make_two_column_pdf()), DOC_ID, "default", {})

    text = " ".join(t for _, t in result.segments)
    assert "左栏内容" in text and "右栏内容" in text, "双栏文字有丢失"
    # 当前实现用 get_text(sort=True)，两栏内容会按几何位置混排。
    # 这里只登记"内容都在"，不断言顺序——顺序断言会在 pymupdf 升级后无故变红。
    sources = [s for s, _ in result.segments]
    assert sources == ["第1页"], f"双栏应仍是一页一段，实际 {sources}"


def test_pdf24_embedded_image_not_extracted(write_pdf, ocr_engine) -> None:
    """PDF-24：**已知缺口登记** —— 文字页内嵌图片**不抽取**。

    矩阵 §1.4 风险 #1：图文混排 PDF 的图信息全丢。
    断言"当前行为"而不是"期望行为"——等哪天补上 image 抽取，这条用例会红，
    那时正好去更新矩阵并把断言改成期望。
    """
    parser = PdfParser(ocr_engine=ocr_engine)
    result = parser.parse(
        write_pdf(make_pdf_with_embedded_image(), "embedded.pdf"), DOC_ID, "default", {}
    )
    assert result.assets == [], (
        f"当前实现不抽取文字页内嵌图，assets 应为空，实际 {result.assets}"
    )
    assert result.segments, "文字内容本身不应受影响"


def test_pdf26_frontend_whitelist_aligned() -> None:
    """PDF-26：`.pdf` 在前后端白名单里都在（真源：router.py 与 file-types.ts）。"""
    from app.services.parsers.router import SUPPORTED_EXTENSIONS

    from .contract import frontend_allowed_extensions

    assert ".pdf" in frontend_allowed_extensions(), "前端白名单缺 .pdf"
    assert ".pdf" in SUPPORTED_EXTENSIONS, "后端白名单缺 .pdf"
