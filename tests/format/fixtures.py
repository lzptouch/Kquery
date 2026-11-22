"""格式测试的**现场造件工厂**。

原则：**不落二进制样本** —— 全部用对应库现场生成，可 diff、可追溯，
且能精确控制"哪些特性被触发"（比如 PDF 的扫描页判定阈值 30 字）。

为什么不落样本文件（矩阵 §十 的核心风险 #8）：
真实世界的畸形文件（双栏、扫描、加密、损坏、超长、CJK 竖排）**从未被测过**，
而现场造件只能造出"理想文件"。本模块尽量覆盖畸形分支（损坏/加密/空/超限），
但**真·加密 OOXML、真·双栏排版、CJK 竖排**仍需 S6 的真实样本层——用例里
对这类不可造的分支一律显式标注"未覆盖"，不假装测过了。

造件器的统一约定：
- 返回 `bytes`（不是 Path）—— 由调用方落盘，避免工厂自己管临时目录；
- 损坏类样本返回**故意坏掉**的字节，用于断言"明确报错"而非"静默空结果"；
- 每个工厂都有 docstring 说明它触发的是哪条分支。
"""
from __future__ import annotations

import io
import json
import zipfile

# --------------------------------------------------------------------------
# PDF（13 类样本，矩阵 §3.3 的 S1~S13）
# --------------------------------------------------------------------------

#: MuPDF 内置简体中文字体名（pymupdf 内置，不需要外部字体文件）
CJK_FONT = "china-s"


def _pdf_text_for(chars_per_page: int, cjk: bool) -> str:
    """造一段**够长**的正文（≥ 扫描页阈值 30 字，否则会被误判成扫描页）。"""
    if cjk:
        seed = "测试内容页面内容知识库检索增强生成"
    else:
        seed = "Kquery knowledge base retrieval augmented generation content"
    return (seed * (chars_per_page // len(seed) + 2))[:chars_per_page]


def _insert_page_lines(page, lines: list[str], cjk: bool, fontsize: int = 11) -> None:
    """逐行插入文本并自动换行（upload_eval 语料用：保证文本层字符数 ≥ 扫描页阈值）。"""
    font = CJK_FONT if cjk else "helv"
    max_chars = 38 if cjk else 80
    wrapped: list[str] = []
    for line in lines:
        while len(line) > max_chars:
            wrapped.append(line[:max_chars])
            line = line[max_chars:]
        wrapped.append(line)
    for i, line in enumerate(wrapped):
        page.insert_text((72, 72 + i * (fontsize + 6)), line, fontsize=fontsize, fontname=font)


def _paginate_body(body: str, lines_per_page: int = 24) -> list[list[str]]:
    """整段正文 → 逐页行列表（`\\f` 显式分页优先，否则按行数均分）。"""
    if "\f" in body:
        return [chunk.splitlines() for chunk in body.split("\f")]
    lines = [ln for ln in body.splitlines()]
    result: list[list[str]] = []
    for i in range(0, len(lines), lines_per_page):
        result.append(lines[i : i + lines_per_page])
    return result or [[]]


def make_text_pdf(
    pages: int = 1,
    chars_per_page: int = 120,
    cjk: bool = True,
    *,
    body: str | None = None,
) -> bytes:
    """S1/S2：文字页 PDF。文本层字符数 ≥ 阈值 → 走文字路径（**不触发 OCR**）。

    传 `body` 时用自定义正文（`\\f` 分页），此时忽略 `pages`/`chars_per_page`。
    """
    import pymupdf

    doc = pymupdf.open()
    if body is not None:
        for lines in _paginate_body(body):
            page = doc.new_page()
            _insert_page_lines(page, lines, cjk)
    else:
        for _ in range(pages):
            page = doc.new_page()
            text = _pdf_text_for(chars_per_page, cjk)
            if cjk:
                page.insert_text((72, 100), text, fontsize=11, fontname=CJK_FONT)
            else:
                page.insert_text((72, 100), text, fontsize=11)
    buf = doc.tobytes()
    doc.close()
    return buf


def _png_bytes(width: int = 400, height: int = 300) -> bytes:
    """一张带边框的位图（作为"扫描页"的内容）。"""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, width - 40, height - 40], outline="black", width=3)
    draw.text((60, 80), "SCANNED PAGE CONTENT", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_text_image(
    lines: list[str], width: int = 640, height: int = 400, fmt: str = "PNG"
) -> bytes:
    """把多行文字渲染成位图（扫描页/图片语料用）。

    优先加载系统中文字体（macOS 的 PingFang / STHeiti）；找不到则退回 PIL
    默认位图字体——此时只保证 ASCII 内容可读，中文会渲染为方框。
    """
    from PIL import Image, ImageDraw, ImageFont

    font = None
    for candidate in (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 22)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()

    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([16, 16, width - 16, height - 16], outline="black", width=2)
    for i, line in enumerate(lines):
        draw.text((36, 36 + i * 34), line, fill="black", font=font)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def make_scanned_pdf(pages: int = 1, *, page_lines: list[list[str]] | None = None) -> bytes:
    """S3/S4：扫描页 PDF —— **无文本层** → 触发逐页扫描判定 → OCR 路径。

    传 `page_lines` 时每页渲染自定义文字（替代默认的 SCANNED PAGE CONTENT）。
    """
    import pymupdf

    doc = pymupdf.open()
    for i in range(pages):
        if page_lines is not None:
            png = _render_text_image(page_lines[i % len(page_lines)])
        else:
            png = _png_bytes()
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(50, 50, 540, 600), stream=png)
    buf = doc.tobytes()
    doc.close()
    return buf


def make_mixed_pdf(
    *, text_body: str | None = None, scan_lines: list[str] | None = None
) -> bytes:
    """S5：混合 PDF —— 第 1 页文字 + 第 2 页扫描 → 验证**逐页独立判定**。

    这是 PDF 解析最关键的行为：一篇里既有文字页又有扫描页时，
    只有扫描页才该走 OCR；整篇一刀切会让文字页的内容丢失或让扫描页拿不到文字。

    `text_body` / `scan_lines` 用于 upload_eval 语料的自定义内容。
    """
    import pymupdf

    doc = pymupdf.open()

    page = doc.new_page()
    if text_body is not None:
        _insert_page_lines(page, text_body.splitlines(), cjk=True)
    else:
        page.insert_text((72, 100), _pdf_text_for(120, True), fontsize=11, fontname=CJK_FONT)

    page2 = doc.new_page()
    png = _render_text_image(scan_lines) if scan_lines else _png_bytes()
    page2.insert_image(pymupdf.Rect(50, 50, 540, 600), stream=png)

    buf = doc.tobytes()
    doc.close()
    return buf


def make_encrypted_pdf() -> bytes:
    """S6：加密 PDF → 触发 `pdf.needs_pass` 分支。"""
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    buf = doc.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner-pw", user_pw="user-pw"
    )
    doc.close()
    return buf


def make_corrupt_pdf() -> bytes:
    """S7：损坏 PDF —— 根本不是 PDF → 触发 `pymupdf.open` 异常分支。"""
    return b"not-a-pdf-at-all" + b"\x00" * 64


def make_empty_pdf() -> bytes:
    """S8：全篇无文本 PDF（1 页空白）→ 触发"未抽取到文本"分支。

    注意不能用 0 页：pymupdf 不允许保存空文档（"cannot save with zero pages"）。
    1 页空白等价——同样没有任何文本层。
    """
    import pymupdf

    doc = pymupdf.open()
    doc.new_page()
    buf = doc.tobytes()
    doc.close()
    return buf


def make_table_pdf(
    rows: int = 3,
    cols: int = 3,
    *,
    cells: list[list[str]] | None = None,
    note: str | None = None,
) -> bytes:
    """S9：带几何表格的 PDF —— 画网格线让 `find_tables()` 能识别。

    `cells` 覆盖默认的 R{r}C{c} 内容（需 rows×cols 对齐）；`note` 覆盖页脚正文。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    x0, y0, cell_w, cell_h = 60, 80, 120, 40
    for r in range(rows):
        for c in range(cols):
            x, y = x0 + c * cell_w, y0 + r * cell_h
            page.draw_rect(pymupdf.Rect(x, y, x + cell_w, y + cell_h), color=(0, 0, 0), width=1)
            text = cells[r][c] if cells else f"R{r}C{c}"
            page.insert_text((x + 8, y + 25), text, fontsize=10, fontname=CJK_FONT)
    # 表格页还需要足够的正文字符量，否则整页会被判成扫描页
    footer = note if note is not None else _pdf_text_for(120, False)
    font = CJK_FONT if any(ord(ch) > 127 for ch in footer) else "helv"
    page.insert_text((60, y0 + rows * cell_h + 40), footer, fontsize=10, fontname=font)
    buf = doc.tobytes()
    doc.close()
    return buf


def make_outline_pdf() -> bytes:
    """S10：3 种字号 + 中文标题 → 触发字号法大纲抽取。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "第一章 总则", fontsize=20, fontname=CJK_FONT)
    page.insert_text((72, 140), "第一节 适用范围", fontsize=14, fontname=CJK_FONT)
    page.insert_text((72, 180), _pdf_text_for(120, True), fontsize=10, fontname=CJK_FONT)
    buf = doc.tobytes()
    doc.close()
    return buf


def make_two_column_pdf() -> bytes:
    """S13：双栏 PDF —— 左右两栏各一段文字。

    断言的是**当前行为**（顺序可能混杂），不是"必须正确"——
    矩阵 §1.4 已把"双栏只缓解不根治"登记为已知限制。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    left = "左栏内容" * 20
    right = "右栏内容" * 20
    page.insert_text((60, 90), left, fontsize=10, fontname=CJK_FONT)
    page.insert_text((320, 90), right, fontsize=10, fontname=CJK_FONT)
    buf = doc.tobytes()
    doc.close()
    return buf


def make_pdf_with_embedded_image() -> bytes:
    """PDF-24：文字页 + 内嵌位图（当前行为：**不抽取**内嵌图，登记为已知缺口）。"""
    import pymupdf

    png = _png_bytes(200, 150)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), _pdf_text_for(120, True), fontsize=11, fontname=CJK_FONT)
    page.insert_image(pymupdf.Rect(72, 140, 272, 290), stream=png)
    buf = doc.tobytes()
    doc.close()
    return buf


# --------------------------------------------------------------------------
# DOCX / Word
# --------------------------------------------------------------------------


def make_docx(
    with_table: bool = True,
    with_image: bool = False,
    headings: int = 2,
    *,
    sections: list[tuple[str, str]] | None = None,
    table_rows: list[list[str]] | None = None,
) -> bytes:
    """DOCX-01/02/04：标题 + 段落（+ 表格，可选嵌入图）。

    默认**带表格**：C5「表格独立成块」是格式层价值最高的契约，
    每种有表格能力的格式，其"标准样本"都应该能走到这条路径，
    否则通用契约测试里这一条对所有格式都是空跑。

    传 `sections`（标题, 正文）列表时用自定义章节；`table_rows` 覆盖默认表格内容。
    """
    from docx import Document
    from docx.shared import Inches

    doc = Document()
    if sections is not None:
        for heading, paragraph in sections:
            doc.add_heading(heading, level=1)
            for para in paragraph.split("\n"):
                if para.strip():
                    doc.add_paragraph(para)
    else:
        for i in range(headings):
            doc.add_heading(f"第{i + 1}章 测试章节", level=1)
            doc.add_paragraph(
                f"这是第 {i + 1} 章的正文段落，包含可检索的特征串 DOCXPARAGRAPH{i + 1}，"
                "用于验证段落被完整保留并进入检索索引。"
            )
    if with_table:
        grid = table_rows or [["R0C0", "R0C1", "R0C2"], ["R1C0", "R1C1", "R1C2"], ["R2C0", "R2C1", "R2C2"]]
        table = doc.add_table(rows=len(grid), cols=len(grid[0]))
        table.style = "Table Grid"
        for r, row in enumerate(grid):
            for c, value in enumerate(row):
                table.cell(r, c).text = str(value)
    if with_image:
        from PIL import Image

        img = Image.new("RGB", (120, 80), "blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        doc.add_picture(io.BytesIO(buf.getvalue()), width=Inches(1.2))

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def make_empty_docx() -> bytes:
    """DOCX-08：空 DOCX（无标题无段落无表格）→ 断言明确报错。"""
    from docx import Document

    doc = Document()
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def make_corrupt_docx() -> bytes:
    """DOCX-06：损坏 DOCX —— 合法 zip 但内容不是 OOXML。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<not-ooxml/>")
        zf.writestr("word/document.xml", "<broken")
    return buf.getvalue()


def make_doc_like_binary() -> bytes:
    """DOCX-09：`.doc` 的**无 LibreOffice** 路径。

    老式 `.doc` 是 OLE 复合文档，无法现场构造；这里给一个不可解析的二进制，
    目的不是"模拟 .doc"，而是让 `.doc` 扩展名在**没有 soffice** 的环境里
    走到 `convert_doc_to_docx` → 明确报「需要 LibreOffice 转换后解析」。
    """
    return b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 128


# --------------------------------------------------------------------------
# XLSX / Excel
# --------------------------------------------------------------------------


def make_xlsx(
    sheets: dict[str, list[list[str]]] | None = None, *, include_default: bool = True
) -> bytes:
    """XLSX-01/02：默认单 sheet 3×3；传 sheets 则多 sheet 展开。

    `include_default=False` 时只保留传入的 sheets（upload_eval 语料用——
    金标事实不需要默认的"套餐"页干扰检索）。
    """
    from openpyxl import Workbook

    def _fill(ws, rows: list[list[str]]) -> None:
        for r, row in enumerate(rows, start=1):
            for c, value in enumerate(row, start=1):
                ws.cell(row=r, column=c, value=value)

    extra = dict(sheets or {})
    wb = Workbook()
    default = wb.active
    if include_default or not extra:
        default.title = "套餐"
        _fill(default, [["知识库套餐", "价格", "说明"], ["基础版", "99", "小规模"], ["专业版", "299", "企业级"]])
    else:
        title, rows = next(iter(extra.items()))
        default.title = title
        _fill(default, rows)
        extra.pop(title)
    for title, rows in extra.items():
        _fill(wb.create_sheet(title), rows)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def make_empty_xlsx() -> bytes:
    """XLSX-07：空工作簿 → 当前行为是 **warning + 空结果**（静默半成功）。"""
    from openpyxl import Workbook

    wb = Workbook()
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def make_corrupt_xlsx() -> bytes:
    """XLSX-08：损坏 XLSX → openpyxl 打开失败 → 3201。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<not-xlsx/>")
    return buf.getvalue()


def make_xlsx_with_long_cell() -> bytes:
    """XLSX-06：超长单元格（> 4000 字）→ 断言截断 + warning。"""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "长单元格"
    ws.cell(row=1, column=1, value="A" * 5000)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


# --------------------------------------------------------------------------
# PPTX / PowerPoint
# --------------------------------------------------------------------------


def make_pptx(
    with_table: bool = True,
    with_notes: bool = False,
    slides: int = 1,
    *,
    sections: list[dict] | None = None,
) -> bytes:
    """PPTX-01/02/03：逐页文本（+ 页内表格，可选备注页）。

    同 `make_docx`：标准样本默认带表格，让 C5 契约对 PPTX 真实生效。

    传 `sections` 时用自定义页面：`{"title": str, "body": str, "table": [[...]]|None, "notes": str|None}`。
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    if sections is not None:
        for sec in sections:
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            slide.shapes.title.text = sec["title"]
            box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
            box.text_frame.text = sec["body"]
            table_rows = sec.get("table")
            if table_rows:
                shape = slide.shapes.add_table(
                    len(table_rows), len(table_rows[0]), Inches(1), Inches(3), Inches(6), Inches(2)
                )
                for r, row in enumerate(table_rows):
                    for c, value in enumerate(row):
                        shape.table.cell(r, c).text = str(value)
            if sec.get("notes"):
                slide.notes_slide.notes_text_frame.text = sec["notes"]
    else:
        for i in range(slides):
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            slide.shapes.title.text = f"演示文稿第 {i + 1} 页"
            box = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1))
            box.text_frame.text = (
                f"这是第 {i + 1} 页的正文，特征串 PPTXSLIDE{i + 1}，用于验证逐页成段与可检索。"
            )
            if with_table:
                shape = slide.shapes.add_table(3, 3, Inches(1), Inches(3), Inches(6), Inches(2))
                for r in range(3):
                    for c in range(3):
                        shape.table.cell(r, c).text = f"R{r}C{c}"
            if with_notes:
                slide.notes_slide.notes_text_frame.text = f"第 {i + 1} 页的备注内容"

    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def make_empty_pptx() -> bytes:
    """PPTX-07：空 PPTX（无幻灯片）→ 当前行为 warning + 空结果。"""
    from pptx import Presentation

    prs = Presentation()
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def make_corrupt_pptx() -> bytes:
    """PPTX-08：损坏 PPTX → 3201。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<not-pptx/>")
    return buf.getvalue()


# --------------------------------------------------------------------------
# 纯文本 / HTML / 代码 / Markdown
# --------------------------------------------------------------------------

TEXT_SAMPLE = (
    "Kquery 知识库问答系统的文本样例。\n"
    "第二行内容用于验证多行文本被完整保留。\n"
    "特征串 PLAINTEXTSAMPLE 应当出现在检索结果中。\n"
)


def make_text(encoding: str = "utf-8", *, text: str | None = None) -> bytes:
    """TXT-01/02/03：UTF-8 / GBK / 非法字节三种编码。`text` 覆盖默认样例。"""
    return (text if text is not None else TEXT_SAMPLE).encode(encoding)


def make_latin1_garbage() -> bytes:
    """TXT-03：非法 UTF-8 字节（latin-1 兜底，**不得抛错**）。"""
    return b"\xff\xfe\x00\x80\x81 plain text fallback \xc3\x28"


def make_empty_text() -> bytes:
    """TXT-04 / MD-03：空文件 → 明确报错。"""
    return b""


def make_markdown(*, body: str | None = None) -> bytes:
    """MD-01：标题层级（# ~ ###）→ 大纲抽取。`body` 覆盖默认正文。"""
    if body is not None:
        return body.encode("utf-8")
    return (
        "# 第一章 总则\n\n"
        "正文段落，包含特征串 MARKDOWNSAMPLE。\n\n"
        "## 第二节 适用范围\n\n"
        "更多正文内容。\n\n"
        "### 第三节 细节\n\n"
        "细节内容。\n"
    ).encode("utf-8")


def make_non_utf8_markdown() -> bytes:
    """MD-02：非 UTF-8 的 Markdown → 明确失败（「不是有效的 UTF-8 编码」）。

    用 GBK 编码的中文：GBK 字节序列绝大多数不是合法 UTF-8，
    触发 `read_text(encoding="utf-8")` 的 UnicodeDecodeError。
    """
    return "# 第一章 总则\n\n这是中文正文。\n".encode("gbk")


HTML_SAMPLE = """<!DOCTYPE html>
<html>
<head><title>标题</title>
<style>body { color: red; }</style>
</head>
<body>
<!-- 这是一条注释，应当被丢弃 -->
<h1>Kquery 文档标题</h1>
<p>正文段落，特征串 HTMLSAMPLE 应当被保留。</p>
<p>第二段内容。</p>
<script>var secret = "SCRIPT_CONTENT_MUST_BE_DROPPED";</script>
</body>
</html>
"""


def make_html(*, body: str | None = None) -> bytes:
    """HTML-01/02/03：含 script / style / 注释的 HTML。`body` 覆盖正文片段。"""
    if body is not None:
        return (
            "<!DOCTYPE html>\n<html>\n<head><title>upload_eval</title></head>\n"
            f"<body>\n{body}\n</body>\n</html>\n"
        ).encode("utf-8")
    return HTML_SAMPLE.encode("utf-8")


def make_code(ext: str, *, body: str | None = None) -> bytes:
    """CODE-01/02：代码文件样本（按纯文本保留全文，不做符号解析）。`body` 覆盖默认。"""
    if body is not None:
        return body.encode("utf-8")
    samples = {
        ".py": "def hello():\n    return 'PYCODE'\n",
        ".js": "function hello() { return 'JSCODE'; }\n",
        ".ts": "export function hello(): string { return 'TSCODE'; }\n",
        ".java": "public class A { String s = \"JAVACODE\"; }\n",
        ".go": "package main\nfunc Hello() string { return \"GOCODE\" }\n",
        ".c": '#include <stdio.h>\nint main(){ printf("CCODE"); }\n',
        ".sql": "SELECT 'SQLCODE' FROM dual;\n",
        ".sh": "#!/bin/bash\necho SHCODE\n",
        ".rb": "def hello; 'RBCODE'; end\n",
        ".rs": 'fn hello() -> &\'static str { "RSCODE" }\n',
        ".php": "<?php function hello(){ return 'PHPCODE'; }\n",
        ".swift": 'func hello() -> String { return "SWIFTCODE" }\n',
        ".scala": 'object A { def hello = "SCALACODE" }\n',
        ".r": 'hello <- function() "RCODE"\n',
        ".kt": 'fun hello(): String = "KTCODE"\n',
        ".cs": 'class A { string s = "CSCODE"; }\n',
        ".cpp": '#include <iostream>\nint main(){ std::cout << "CPPCODE"; }\n',
        ".h": '#define HCODE 1\n',
        ".hpp": "inline int hpp_code() { return 1; }\n",
        ".lua": 'function hello() return "LUACODE" end\n',
        ".pl": 'my $x = "PLCODE";\n',
        ".jsx": "export const A = () => <div>JSXCODE</div>;\n",
        ".tsx": "export const A = (): JSX.Element => <div>TSXCODE</div>;\n",
        ".vue": "<template><div>VUE CODE</div></template>\n",
        ".svelte": "<script>let s = 'SVELTE CODE';</script>\n",
        ".bash": "echo BASHCODE\n",
        ".zsh": "echo ZSHCODE\n",
        ".m": '@interface A : NSObject\n// MCODE objective-c\n@end\n',
    }
    body = samples.get(ext, f"generic code sample for {ext}\nCODE SAMPLE\n")
    return body.encode("utf-8")


# --------------------------------------------------------------------------
# 图片（OCR）
# --------------------------------------------------------------------------


def make_png(*, lines: list[str] | None = None) -> bytes:
    """IMG：PNG 位图（走 OCR 路径）。`lines` 覆盖默认文字。"""
    if lines is not None:
        return _render_text_image(lines, fmt="PNG")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 300, 220], outline="black", width=2)
    draw.text((40, 60), "IMAGE OCR SAMPLE", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_jpg(*, lines: list[str] | None = None) -> bytes:
    """IMG：JPEG 位图。`lines` 覆盖默认文字。"""
    if lines is not None:
        return _render_text_image(lines, fmt="JPEG")
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (320, 240), "white")
    draw = ImageDraw.Draw(img)
    draw.text((40, 60), "IMAGE OCR SAMPLE JPEG", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def make_jpeg() -> bytes:
    """IMG：JPEG 的另一扩展名（.jpeg）。"""
    return make_jpg()


def make_json() -> bytes:
    """TXT-06：JSON（pretty-print 路径）。"""
    return json.dumps({"名称": "Kquery", "版本": "1.0", "功能": ["检索", "问答"]}, ensure_ascii=False).encode("utf-8")


def make_csv() -> bytes:
    """TXT-07：CSV。"""
    return "名称,版本,说明\nKquery,1.0,知识库问答\n".encode("utf-8")


def make_yaml() -> bytes:
    """TXT-08：YAML。"""
    return "name: Kquery\nversion: '1.0'\nfeatures:\n  - search\n  - qa\n".encode("utf-8")


def make_toml() -> bytes:
    """TXT-08：TOML。"""
    return 'name = "Kquery"\nversion = "1.0"\n'.encode("utf-8")


def make_ini() -> bytes:
    """TXT-08：INI。"""
    return "[kquery]\nname=Kquery\nversion=1.0\n".encode("utf-8")


def make_log() -> bytes:
    """TXT-08：LOG。"""
    return "2026-10-30 10:00:00 INFO kquery started\n2026-10-30 10:00:01 INFO ready\n".encode("utf-8")


def make_tsv() -> bytes:
    """TXT-07：TSV。"""
    return "名称\t版本\t说明\nKquery\t1.0\t知识库问答\n".encode("utf-8")


def make_jsonl() -> bytes:
    """TXT-08：JSONL（多行 JSON 对象）。"""
    return b'{"a":1}\n{"b":2}\n'


# --------------------------------------------------------------------------
# 全扩展名注册表：50 个扩展名 → (造件器, 预期解析器名, 格式族)
# --------------------------------------------------------------------------

#: 格式族名（对应矩阵 §1.1 的 9 个格式分组）
FMT_PDF = "pdf"
FMT_MARKDOWN = "markdown"
FMT_DOCX = "docx"
FMT_XLSX = "xlsx"
FMT_PPTX = "pptx"
FMT_PLAIN = "plaintext"
FMT_CODE = "code"
FMT_IMAGE = "image"


#: 期望的 `ParserRouter.route()` 返回值
#: 纯文本族与代码族共用 `PlainTextParser` → 路由名同为 "plaintext"
EXPECTED_PARSER = {
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
    ".docx": "docx",
    ".doc": "docx",
    ".xlsx": "spreadsheet",
    ".pptx": "presentation",
    ".png": "ocr",
    ".jpg": "ocr",
    ".jpeg": "ocr",
}

#: 文本族扩展名（与 plaintext.py 的 TEXT/HTML 扩展名一致）
TEXT_LIKE = {
    ".txt": make_text,
    ".csv": make_csv,
    ".tsv": make_tsv,
    ".json": make_json,
    ".jsonl": make_jsonl,
    ".yaml": make_yaml,
    ".yml": make_yaml,
    ".log": make_log,
    ".ini": make_ini,
    ".toml": make_toml,
    ".html": make_html,
    ".htm": make_html,
}

#: 代码族扩展名（与 plaintext.py 的 CODE_EXTENSIONS 一致）
CODE_LIKE = {ext: (lambda e=ext: make_code(e)) for ext in (
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".kt", ".swift", ".scala", ".sh", ".bash",
    ".zsh", ".sql", ".r", ".m", ".lua", ".pl", ".vue", ".svelte",
)}

#: **全部 50 个扩展名** → 造件器
ALL_EXTENSION_SAMPLES: dict[str, object] = {
    ".pdf": make_text_pdf,
    ".md": make_markdown,
    ".markdown": make_markdown,
    ".docx": lambda: make_docx(),
    ".doc": make_doc_like_binary,
    ".xlsx": make_xlsx,
    ".pptx": lambda: make_pptx(),
    **{ext: builder for ext, builder in TEXT_LIKE.items()},
    **{ext: builder for ext, builder in CODE_LIKE.items()},
    ".png": make_png,
    ".jpg": make_jpg,
    ".jpeg": make_jpeg,
}

#: 扩展名 → 格式族
EXTENSION_FORMAT = {
    ".pdf": FMT_PDF,
    ".md": FMT_MARKDOWN,
    ".markdown": FMT_MARKDOWN,
    ".docx": FMT_DOCX,
    ".doc": FMT_DOCX,
    ".xlsx": FMT_XLSX,
    ".pptx": FMT_PPTX,
    **{ext: FMT_PLAIN for ext in TEXT_LIKE},
    **{ext: FMT_CODE for ext in CODE_LIKE},
    ".png": FMT_IMAGE,
    ".jpg": FMT_IMAGE,
    ".jpeg": FMT_IMAGE,
}


def sample_bytes(ext: str) -> bytes:
    """按扩展名造一个样本文件。"""
    builder = ALL_EXTENSION_SAMPLES[ext]
    return builder()  # type: ignore[operator]


#: 纯文本族与代码族共用 `PlainTextParser` → 路由名同为 "plaintext"
EXPECTED_PARSER.update({ext: "plaintext" for ext in TEXT_LIKE})
EXPECTED_PARSER.update({ext: "plaintext" for ext in CODE_LIKE})
