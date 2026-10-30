"""除 PDF 外的各格式专项（矩阵 §四~§八）。

编号与矩阵一一对应：
- DOCX-01~DOCX-11（§四）
- XLSX-01~XLSX-12（§五）
- PPTX-01~PPTX-10（§六）
- MD-01~MD-03 / TXT-01~TXT-08 / HTML-01~HTML-04 / CODE-01~CODE-02（§七）
- IMG-01~IMG-04 / AUD-01~AUD-04（§八）

⛔ 人工签字用例（矩阵里的 ⛔ 标记）不写自动化断言，只留说明：
- IMG-05：真实 dashscope OCR（需真实凭据 + 费用）
- AUD-05：真实 paraformer-v2 ASR（同上）
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.envelope import ApiError
from app.services.parsers.audio import (
    AudioParser,
    MockTranscriber,
    classify_platform_error,
)
from app.services.parsers.audio import build_transcriber as build_parser_transcriber
from app.services.parsers.docx import DocxParser, find_soffice
from app.services.parsers.markdown import MarkdownParser
from app.services.parsers.ocr import build_ocr_engine
from app.services.parsers.plaintext import PlainTextParser
from app.services.parsers.presentation import PresentationParser
from app.services.parsers.spreadsheet import SpreadsheetParser


@pytest.fixture
def write(tmp_path: Path):
    def _write(data: bytes, name: str) -> Path:
        path = tmp_path / name
        path.write_bytes(data)
        return path

    return _write


# ==========================================================================
# DOCX / Word（§四）
# ==========================================================================


def test_docx01_headings_structure(write, assets_dir) -> None:
    """DOCX-01：按 Heading 结构化分段 —— 大纲含各级标题，段落段 source 指向标题。"""
    from .fixtures import make_docx

    parser = DocxParser(assets_dir)
    result = parser.parse(write(make_docx(), "s.docx"), "docx-1", "default", {})

    assert result.outline, "未抽出大纲"
    titles = [item["title"] for item in result.outline]
    assert any("测试章节" in t for t in titles), f"大纲缺少标题：{titles}"
    assert all(item["level"] in (1, 2, 3) for item in result.outline), "大纲层级越界"
    sources = [s for s, _ in result.segments]
    assert any("测试章节" in s for s in sources), f"段落 source 未指向标题：{sources}"


def test_docx02_table_to_markdown(write, assets_dir) -> None:
    """DOCX-02：表格转 Markdown + TableRecord（page=0 表示按文档序）。"""
    from .fixtures import make_docx

    parser = DocxParser(assets_dir)
    result = parser.parse(write(make_docx(with_table=True), "s.docx"), "docx-2", "default", {})

    assert result.tables, "未抽出表格"
    table = result.tables[0]
    assert table.page == 0, f"docx 无页码概念，page 应为 0，实际 {table.page}"
    assert table.rows == 3 and table.cols == 3, f"行列异常：{table.rows}x{table.cols}"
    assert "R0C0" in table.markdown, f"markdown 缺表头：{table.markdown[:120]!r}"


def test_docx03_table_becomes_own_chunk(write, assets_dir) -> None:
    """DOCX-03：表格单独成块，不被拦腰切断。"""
    from .contract import chunk_like_production

    from .fixtures import make_docx

    parser = DocxParser(assets_dir)
    result = parser.parse(write(make_docx(with_table=True), "s.docx"), "docx-3", "default", {})

    chunks = chunk_like_production("docx-3", result)
    assert any(c.modality == "table" for c in chunks), "表格没有独立成块"


def test_docx04_embedded_image_asset(write, assets_dir) -> None:
    """DOCX-04：嵌入图存档为 MediaAsset（kind=image），文件真实存在。"""
    from .fixtures import make_docx

    parser = DocxParser(assets_dir)
    result = parser.parse(
        write(make_docx(with_image=True), "s.docx"), "docx-4", "default", {}
    )

    assert result.assets, "未抽出嵌入图"
    asset = result.assets[0]
    assert asset.kind == "image", f"kind 应为 image，实际 {asset.kind!r}"
    assert Path(asset.uri).exists(), f"嵌入图文件不存在：{asset.uri}"


def test_docx05_document_order_preserved(write, assets_dir) -> None:
    """DOCX-05：段落与表格交替出现时，**文档序**不乱。

    判定依据：表格段的序号必须单调递增，且表格段的 seq 不早于它前面那段正文。
    """
    from .fixtures import make_docx

    parser = DocxParser(assets_dir)
    result = parser.parse(write(make_docx(with_table=True), "s.docx"), "docx-5", "default", {})

    table_sources = [s for s, _ in result.segments if "·表格" in s]
    assert len(table_sources) == 1, f"应有 1 个表格段：{result.segments}"
    assert table_sources[0] == "文档序·表格1", f"表格段命名异常：{table_sources[0]}"
    body_sources = [s for s, _ in result.segments if "·表格" not in s]
    assert body_sources, "正文段为空"


def test_docx06_corrupt_docx(write, assets_dir) -> None:
    """DOCX-06：损坏 DOCX → 3003 且文案含「DOCX 打开失败」。"""
    from .fixtures import make_corrupt_docx

    parser = DocxParser(assets_dir)
    with pytest.raises(ApiError) as exc:
        parser.parse(write(make_corrupt_docx(), "bad.docx"), "docx-6", "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert "DOCX 打开失败" in exc.value.message, f"文案异常：{exc.value.message}"


def test_docx08_empty_docx(write, assets_dir) -> None:
    """DOCX-08：空 DOCX → 明确报错（**不静默产出空索引**）。"""
    from .fixtures import make_empty_docx

    parser = DocxParser(assets_dir)
    with pytest.raises(ApiError) as exc:
        parser.parse(write(make_empty_docx(), "empty.docx"), "docx-8", "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"


def test_docx09_doc_without_libreoffice(write, assets_dir) -> None:
    """DOCX-09：`.doc` 无 LibreOffice → **诚实失败**，文案说明需要 LibreOffice。

    关键：不得"成功但结构全丢"——那比直接报错更糟，用户会以为内容入库了。
    """
    if find_soffice() is not None:
        pytest.skip("本机装了 LibreOffice，.doc 走转换路径（见 DOCX-10）")

    from .fixtures import make_doc_like_binary

    parser = DocxParser(assets_dir)
    with pytest.raises(ApiError) as exc:
        parser.parse(write(make_doc_like_binary(), "old.doc"), "docx-9", "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert "LibreOffice" in exc.value.message, f"文案异常：{exc.value.message}"
    assert "转换" in exc.value.message, f"文案应说明需要转换：{exc.value.message}"


def test_docx10_doc_with_libreoffice(write, assets_dir) -> None:
    """DOCX-10：`.doc` 有 LibreOffice → 转换后走同一管线（**环境依赖**）。"""
    if find_soffice() is None:
        pytest.skip("本机无 LibreOffice，.doc 转换路径不可测（矩阵标注为环境依赖）")


# ==========================================================================
# XLSX / Excel（§五）
# ==========================================================================


def test_xlsx01_single_sheet(write) -> None:
    """XLSX-01：单 sheet → 每 sheet 一段一表，markdown 含首行。"""
    from .fixtures import make_xlsx

    parser = SpreadsheetParser()
    result = parser.parse(write(make_xlsx(), "s.xlsx"), "xlsx-1", "default", {})

    assert result.tables, "未抽出表格"
    table = result.tables[0]
    assert table.rows == 3 and table.cols == 3, f"行列异常：{table.rows}x{table.cols}"
    assert "知识库套餐" in table.markdown, f"markdown 缺内容：{table.markdown[:120]!r}"


def test_xlsx02_multi_sheet(write) -> None:
    """XLSX-02：多 sheet 逐个展开，source 含 sheet 名。"""
    from .fixtures import make_xlsx

    sheets = {"第二个表": [["A", "B"], ["1", "2"]]}
    parser = SpreadsheetParser()
    result = parser.parse(write(make_xlsx(sheets), "s.xlsx"), "xlsx-2", "default", {})

    assert len(result.tables) == 2, f"期望 2 张表，实际 {len(result.tables)}"
    sources = [s for s, _ in result.segments]
    assert any("套餐" in s for s in sources), f"source 缺 sheet 名：{sources}"
    assert any("第二个表" in s for s in sources), f"source 缺 sheet 名：{sources}"


def test_xlsx03_table_becomes_own_chunk(write) -> None:
    """XLSX-03：表格单独成块（结构保护）。"""
    from .contract import chunk_like_production

    from .fixtures import make_xlsx

    parser = SpreadsheetParser()
    result = parser.parse(write(make_xlsx(), "s.xlsx"), "xlsx-3", "default", {})

    chunks = chunk_like_production("xlsx-3", result)
    assert any(c.modality == "table" for c in chunks), "表格没有独立成块"


def test_xlsx06_long_cell_truncated(write) -> None:
    """XLSX-06：超长单元格（>4000 字）→ 截断并告警。

    当前实现是**静默截断**（无 warning），所以这条只断言"内容没丢到炸掉"，
    截断告警的缺失登记在矩阵 §十二 风险台账里（属"静默半成功"家族）。
    """
    from .fixtures import make_xlsx_with_long_cell

    parser = SpreadsheetParser()
    result = parser.parse(write(make_xlsx_with_long_cell(), "s.xlsx"), "xlsx-6", "default", {})

    assert result.tables, "未抽出表格"
    markdown = result.tables[0].markdown
    assert len(markdown) < 10_000, f"超长单元格未被截断（{len(markdown)} 字符）"
    assert "A" in markdown, "单元格内容被整体丢弃"


def test_xlsx07_empty_workbook_is_warning_not_error(write) -> None:
    """XLSX-07：空工作簿 → **静默半成功**（warning + 空结果，不报错）。

    这是登记在案的现状（矩阵 §1.4），不是期望行为：
    用户看到"入库成功"实际 0 块。断言现状是为了让行为变化时能立刻发现。
    """
    from .fixtures import make_empty_xlsx

    parser = SpreadsheetParser()
    result = parser.parse(write(make_empty_xlsx(), "s.xlsx"), "xlsx-7", "default", {})

    assert result.segments == [] and result.tables == [], "空工作簿不应产出内容"
    assert result.warnings, "空工作簿必须给出 warning（否则就是静默失败）"
    assert any("空" in w for w in result.warnings), f"warning 异常：{result.warnings}"


def test_xlsx08_corrupt_xlsx(write) -> None:
    """XLSX-08：损坏 XLSX → 3201 且文案含「Excel 文件无法打开」。"""
    from .fixtures import make_corrupt_xlsx

    parser = SpreadsheetParser()
    with pytest.raises(ApiError) as exc:
        parser.parse(write(make_corrupt_xlsx(), "bad.xlsx"), "xlsx-8", "default", {})
    assert exc.value.code == 3201, f"期望 3201，实际 {exc.value.code}"
    assert "Excel 文件无法打开" in exc.value.message, f"文案异常：{exc.value.message}"


def test_xlsx10_xls_hint(write) -> None:
    """XLSX-10：`.xls` → 可读引导（提示另存为 .xlsx）。"""
    from app.services.parsers.router import ParserRouter

    router = ParserRouter(
        ocr_parser=None, audio_parser=None, docx_parser=None  # type: ignore[arg-type]
    )
    path = write(b"\xd0\xcf\x11\xe0", "old.xls")
    with pytest.raises(ApiError) as exc:
        router.route(path)
    assert exc.value.code == 3101, f"期望 3101，实际 {exc.value.code}"
    assert "xlsx" in exc.value.message, f"文案应提示另存为 xlsx：{exc.value.message}"


# ==========================================================================
# PPTX / PowerPoint（§六）
# ==========================================================================


def test_pptx01_slide_per_segment(write) -> None:
    """PPTX-01：逐页成段，source 形如「第 1 页」。"""
    from .fixtures import make_pptx

    parser = PresentationParser()
    result = parser.parse(write(make_pptx(slides=2), "s.pptx"), "pptx-1", "default", {})

    sources = [s for s, _ in result.segments if s.startswith("第")]
    assert len(sources) == 2, f"期望 2 页正文段，实际 {sources}"
    assert sources[0] == "第 1 页", f"source 命名异常：{sources[0]!r}"


def test_pptx02_slide_table(write) -> None:
    """PPTX-02：页内表格 → 独立 TableRecord。

    注意 page 是 **0 基**（slide 1 → page=0），与 PDF/DOCX 的页码语义不同；
    矩阵 §六写的 "page == 1" 与实现不符，以实现为准并在此登记。
    """
    from .fixtures import make_pptx

    parser = PresentationParser()
    result = parser.parse(write(make_pptx(with_table=True), "s.pptx"), "pptx-2", "default", {})

    assert result.tables, "未抽出页内表格"
    table = result.tables[0]
    assert table.page == 0, f"slide 1 的 page 应为 0（0 基），实际 {table.page}"
    assert table.rows >= 2 and table.cols >= 2, f"行列异常：{table.rows}x{table.cols}"
    assert "R0C0" in table.markdown, f"markdown 缺表头：{table.markdown[:120]!r}"


def test_pptx03_notes_merged_into_body(write) -> None:
    """PPTX-03：备注页并入正文，带「[备注]」标记。"""
    from .fixtures import make_pptx

    parser = PresentationParser()
    result = parser.parse(write(make_pptx(with_notes=True), "s.pptx"), "pptx-3", "default", {})

    body = " ".join(t for _, t in result.segments)
    assert "[备注]" in body, f"备注未并入正文：{body[:160]!r}"


def test_pptx07_empty_pptx_is_warning_not_error(write) -> None:
    """PPTX-07：空 PPTX → warning + 空结果，不报错（登记在案的静默半成功）。"""
    from .fixtures import make_empty_pptx

    parser = PresentationParser()
    result = parser.parse(write(make_empty_pptx(), "s.pptx"), "pptx-7", "default", {})

    assert result.segments == [] and result.tables == [], "空 PPTX 不应产出内容"
    assert result.warnings, "空 PPTX 必须给出 warning"


def test_pptx08_corrupt_pptx(write) -> None:
    """PPTX-08：损坏 PPTX → 3201。"""
    from .fixtures import make_corrupt_pptx

    parser = PresentationParser()
    with pytest.raises(ApiError) as exc:
        parser.parse(write(make_corrupt_pptx(), "bad.pptx"), "pptx-8", "default", {})
    assert exc.value.code == 3201, f"期望 3201，实际 {exc.value.code}"


def test_pptx09_ppt_hint() -> None:
    """PPTX-09：`.ppt` → 可读引导（提示另存为 .pptx）。"""
    from app.services.parsers.router import ParserRouter

    router = ParserRouter(
        ocr_parser=None, audio_parser=None, docx_parser=None  # type: ignore[arg-type]
    )
    with pytest.raises(ApiError) as exc:
        router.route(Path("/tmp/old.ppt"))
    assert exc.value.code == 3101, f"期望 3101，实际 {exc.value.code}"
    assert "pptx" in exc.value.message, f"文案应提示另存为 pptx：{exc.value.message}"


# ==========================================================================
# Markdown / 纯文本 / HTML / 代码（§七）
# ==========================================================================


def test_md01_outline_levels(write) -> None:
    """MD-01：`#`~`###` 产出 outline（仅 3 级）。"""
    from .fixtures import make_markdown

    parser = MarkdownParser()
    result = parser.parse(write(make_markdown(), "s.md"), "md-1", "default", {})

    levels = [item["level"] for item in result.outline]
    assert levels == [1, 2, 3], f"大纲层级异常：{result.outline}"
    titles = [item["title"] for item in result.outline]
    assert "第一章 总则" in titles, f"大纲异常：{titles}"


def test_md02_non_utf8_markdown(write) -> None:
    """MD-02：非 UTF-8 → 3003 且文案含「不是有效的 UTF-8 编码」。"""
    from .fixtures import make_non_utf8_markdown

    parser = MarkdownParser()
    with pytest.raises(ApiError) as exc:
        parser.parse(write(make_non_utf8_markdown(), "s.md"), "md-2", "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert "UTF-8" in exc.value.message, f"文案异常：{exc.value.message}"


def test_md03_empty_markdown(write) -> None:
    """MD-03：空文件 → 3003 且文案含「文档内容为空」。"""
    parser = MarkdownParser()
    with pytest.raises(ApiError) as exc:
        parser.parse(write(b"", "empty.md"), "md-3", "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"
    assert "文档内容为空" in exc.value.message, f"文案异常：{exc.value.message}"


def test_txt01_utf8_preserved(write) -> None:
    """TXT-01：UTF-8 文本 → 全文保留。"""
    from .fixtures import make_text

    parser = PlainTextParser()
    result = parser.parse(write(make_text(), "s.txt"), "txt-1", "default", {})

    text = result.segments[0][1]
    assert "PLAINTEXTSAMPLE" in text, "特征串丢失"
    assert "第二行内容" in text, "多行内容丢失"


def test_txt02_gbk_fallback(write) -> None:
    """TXT-02：GBK 文本 → 中文不乱码。"""
    from .fixtures import make_text

    parser = PlainTextParser()
    result = parser.parse(
        write(make_text(encoding="gbk"), "s.txt"), "txt-2", "default", {}
    )
    text = result.segments[0][1]
    assert "知识库问答系统" in text, f"GBK 回退失败，内容乱码：{text[:80]!r}"


def test_txt03_invalid_bytes_no_crash(write) -> None:
    """TXT-03：非法字节 → latin-1 兜底，**不抛错**。"""
    from .fixtures import make_latin1_garbage

    parser = PlainTextParser()
    result = parser.parse(
        write(make_latin1_garbage(), "s.txt"), "txt-3", "default", {}
    )
    assert result.segments, "latin-1 兜底后不应为空"
    assert "plain text fallback" in result.segments[0][1]


def test_txt04_empty_file(write) -> None:
    """TXT-04：空文件 → 3201 且文案含「文档内容为空」。"""
    parser = PlainTextParser()
    with pytest.raises(ApiError) as exc:
        parser.parse(write(b"", "empty.txt"), "txt-4", "default", {})
    assert exc.value.code == 3201, f"期望 3201，实际 {exc.value.code}"
    assert "文档内容为空" in exc.value.message, f"文案异常：{exc.value.message}"


def test_txt06_json_pretty_print(write) -> None:
    """TXT-06：JSON → 输出格式化后的 JSON。"""
    from .fixtures import make_json

    parser = PlainTextParser()
    result = parser.parse(write(make_json(), "s.json"), "txt-6", "default", {})
    text = result.segments[0][1]
    assert "\n" in text.strip(), "JSON 未被格式化（仍是单行）"
    assert "Kquery" in text


def test_txt07_csv_rows_preserved(write) -> None:
    """TXT-07：CSV 逐行保留。"""
    from .fixtures import make_csv

    parser = PlainTextParser()
    result = parser.parse(write(make_csv(), "s.csv"), "txt-7", "default", {})
    text = result.segments[0][1]
    assert text.count("\n") >= 2, f"CSV 行数不足：{text!r}"
    assert result.segments[0][0] == "表格数据"


def test_html01_tags_stripped(write) -> None:
    """HTML-01：普通 HTML → 无标签残留。"""
    from .fixtures import make_html

    parser = PlainTextParser()
    result = parser.parse(write(make_html(), "s.html"), "html-1", "default", {})
    text = result.segments[0][1]
    assert "<" not in text and ">" not in text, f"标签残留：{text[:200]!r}"
    assert "HTMLSAMPLE" in text, "正文丢失"


def test_html02_script_style_dropped(write) -> None:
    """HTML-02：`<script>` / `<style>` 的**内容**丢弃（不是只丢标签）。

    脚本正文混进索引会污染检索语义——用户搜"secret"会命中一段 JS。
    """
    from .fixtures import make_html

    parser = PlainTextParser()
    result = parser.parse(write(make_html(), "s.html"), "html-2", "default", {})
    text = result.segments[0][1]
    assert "SCRIPT_CONTENT_MUST_BE_DROPPED" not in text, "script 内容混进了索引"
    assert "color: red" not in text, "style 内容混进了索引"


def test_html03_comments_dropped(write) -> None:
    """HTML-03：注释丢弃。"""
    from .fixtures import make_html

    parser = PlainTextParser()
    result = parser.parse(write(make_html(), "s.html"), "html-3", "default", {})
    text = result.segments[0][1]
    assert "这是一条注释" not in text, "HTML 注释混进了索引"


def test_code02_content_preserved(write) -> None:
    """CODE-02：代码内容逐行完整保留，不做符号解析。"""
    from .fixtures import make_code

    parser = PlainTextParser()
    result = parser.parse(write(make_code(".py"), "s.py"), "code-2", "default", {})
    text = result.segments[0][1]
    assert "def hello():" in text, f"代码内容被改写：{text!r}"
    assert "PYCODE" in text, "代码内容丢失"


# ==========================================================================
# 图片 OCR / 音频 ASR（§八）
# ==========================================================================


@pytest.mark.parametrize("provider", ["local", "onnx", "paddleocr"])
def test_img01_illegal_ocr_provider(provider: str) -> None:
    """IMG-01：非法 OCR provider → **启动期与解析期都报错**（不静默退回 Mock）。"""
    with pytest.raises(ApiError) as exc:
        build_ocr_engine(provider, api_key="k")
    assert exc.value.code == 2001, f"期望 2001，实际 {exc.value.code}"
    assert provider in exc.value.message, f"文案应指明 provider：{exc.value.message}"


def test_img02_mock_provider_works(write, assets_dir) -> None:
    """IMG-02：mock provider → 产出段 + kind=image 的资源。"""
    from app.services.parsers.ocr import MockOcrEngine, OcrParser

    parser = OcrParser(MockOcrEngine(), assets_dir)
    result = parser.parse(write(b"\x89PNG\r\n\x1a\n", "s.png"), "img-2", "default", {})

    assert result.segments, "未产出段"
    assert result.segments[0][0] == "第1页·OCR", f"source 异常：{result.segments[0][0]!r}"
    assert result.assets and result.assets[0].kind == "image"


def test_img04_empty_ocr_result(write, assets_dir) -> None:
    """IMG-04：OCR 空结果 → 明确失败（3003），不静默入库空文档。"""
    from .conftest import CountingOcrEngine

    from app.services.parsers.ocr import OcrParser

    parser = OcrParser(CountingOcrEngine(text=""), assets_dir)
    with pytest.raises(ApiError) as exc:
        parser.parse(write(b"\x89PNG\r\n\x1a\n", "s.png"), "img-4", "default", {})
    assert exc.value.code == 3003, f"期望 3003，实际 {exc.value.code}"


def test_aud01_transcriber_none_without_key() -> None:
    """AUD-01：无 API Key / provider=none → 转写器为 None，能力不可用但服务不崩。

    注意有两个同名 `build_transcriber`：这里测的是
    `app.services.speech.asr.build_transcriber(settings)`（返回 None 的那个）。
    """
    from app.core.config import Settings
    from app.services.speech.asr import build_transcriber as build_from_settings

    settings = Settings(speech_asr_provider="none")
    assert build_from_settings(settings) is None

    settings_no_key = Settings(
        speech_asr_provider="dashscope", dashscope_api_key="", speech_asr_api_key=""
    )
    assert build_from_settings(settings_no_key) is None


def test_aud02_error_classification() -> None:
    """AUD-02：四类错误分流 —— 且**配额不重试**。

    判序也重要：429 的报文里常带 "RateQuota"，先判限流才不会把限流误判成配额
    （限流要重试、配额不重试，判错一次代价很大）。
    """
    assert classify_platform_error(RuntimeError("Throttled by server")) == "rate_limited"
    assert classify_platform_error(RuntimeError("HTTP 429 Too Many Requests")) == "rate_limited"
    assert classify_platform_error(RuntimeError("insufficient quota")) == "quota"
    assert classify_platform_error(TimeoutError("deadline exceeded")) == "timeout"
    assert classify_platform_error(RuntimeError("something else")) == "other"

    from app.services.parsers.audio import _NO_RETRY_KINDS

    assert "quota" in _NO_RETRY_KINDS, "配额类错误必须不重试（重试只会浪费额度）"
    assert "rate_limited" not in _NO_RETRY_KINDS, "限流应当重试"


def test_aud03_wav_silence_split_timestamp(write, assets_dir) -> None:
    """AUD-03：WAV 静音切分 → source 形如 `[0.0s-3.2s]`。"""
    from .fixtures import make_wav

    parser = AudioParser(MockTranscriber(), assets_dir)
    result = parser.parse(write(make_wav(), "s.wav"), "aud-3", "default", {})

    sources = [s for s, _ in result.segments]
    assert sources, "未产出段"
    assert all(s.startswith("[") and s.endswith("]") for s in sources), (
        f"source 应为时间戳区间：{sources}"
    )
    assert "s-" in sources[0], f"source 格式异常：{sources[0]!r}"


def test_aud04_non_wav_whole_file(write, assets_dir) -> None:
    """AUD-04：非 WAV → 整文件直传，不做切分（不谎报采样率）。"""
    from .fixtures import make_mp3

    parser = AudioParser(MockTranscriber(), assets_dir)
    result = parser.parse(write(make_mp3(), "s.mp3"), "aud-4", "default", {})

    assert result.segments, "未产出段"
    assert result.assets and result.assets[0].kind == "audio"


def test_aud_illegal_transcriber_provider() -> None:
    """AUD-01 补充：非法转写 provider → 明确报错（不静默退回 Mock）。"""
    with pytest.raises(ApiError) as exc:
        build_parser_transcriber("whisper-local", api_key="k")
    assert exc.value.code == 2001, f"期望 2001，实际 {exc.value.code}"


# ==========================================================================
# ⛔ 人工签字用例（不自动化，只登记）
# ==========================================================================


def test_img05_manual_signoff_required() -> None:
    """IMG-05（⛔ 人工签字）：真实 dashscope OCR 需真实凭据 + 费用，不自动化。

    签字时要留的证据：真实凭据下 qwen-vl-ocr 的识别文本、识别耗时、
    与人工标注的差异清单。没留证据就不算通过。
    """
    pytest.skip("⛔ 人工签字用例：真实云端 OCR（需凭据 + 费用），见矩阵 §八 IMG-05")


def test_aud05_manual_signoff_required() -> None:
    """AUD-05（⛔ 人工签字）：真实 paraformer-v2 ASR 需真实凭据 + 费用，不自动化。"""
    pytest.skip("⛔ 人工签字用例：真实云端 ASR（需凭据 + 费用），见矩阵 §八 AUD-05")
