"""调研驱动的解析增强测试（parsing-tech-survey 附录的落地验证）。

- DOCX：Heading 样式大纲 + 按标题结构分段 + 表格单独成块 + 嵌入图存档 + 文档序不破坏；
- .doc：无 LibreOffice 时诚实失败（不静默降级）；
- PDF：字号法大纲提取；
- 结构化切块价值：标题分段可被精准召回。
"""
import io

from docx import Document


def _make_docx() -> bytes:
    doc = Document()
    doc.add_heading("电池充电规范", level=1)
    doc.add_paragraph("XB-2024 电池的充电电流不得超过 10A，充电环境温度应保持在 0 到 45 度之间。")
    doc.add_heading("电池存放要求", level=1)
    doc.add_paragraph("长期存放电池时应将电量保持在 40% 到 60% 之间，并放置于阴凉干燥处。")
    # 表格：规格参数
    table = doc.add_table(rows=3, cols=2)
    table.style = "Table Grid"
    table.rows[0].cells[0].text = "参数"
    table.rows[0].cells[1].text = "数值"
    table.rows[1].cells[0].text = "标称电压"
    table.rows[1].cells[1].text = "24V"
    table.rows[2].cells[0].text = "容量"
    table.rows[2].cells[1].text = "5000mAh"
    # 嵌入图
    from PIL import Image

    img = Image.new("RGB", (60, 40), "blue")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    doc.add_picture(buf)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


class TestDocxParser:
    def test_docx_outline_sections_tables_images(self, client):
        resp = client.post("/knowledge/upload", files={"file": ("规范.docx", _make_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        data = resp.json()["data"]
        assert data["status"] == "indexed", data

        preview = client.get("/knowledge/preview/规范").json()["data"]
        # ① Heading 样式 → 大纲
        titles = [o["title"] for o in preview["outline"]]
        assert "电池充电规范" in titles and "电池存放要求" in titles
        # ② 按标题结构分段（source 为标题）
        sources = {c["source"] for c in preview["chunks"]}
        assert any("电池充电规范" in s for s in sources)
        # ③ 表格单独成块（modality=table）+ 行列结构另存
        table_chunks = [c for c in preview["chunks"] if c["modality"] == "table"]
        assert table_chunks, "表格应单独成块"
        assert preview["tables"] and preview["tables"][0]["rows"] == 3
        assert preview["tables"][0]["cols"] == 2
        assert "24V" in preview["tables"][0]["markdown"]
        # ④ 嵌入图存档为 MediaAsset
        assert preview["assets"] and preview["assets"][0]["kind"] == "image"

    def test_docx_content_retrievable(self, client):
        client.post("/knowledge/upload", files={"file": ("规范.docx", _make_docx(), "application/octet-stream")})
        data = client.post("/ask", json={"question": "电池充电电流不能超过多少？"}).json()["data"]
        assert "10A" in str(data["evidence"][0]["content"]) or "10A" in data["answer"]

    def test_doc_without_libreoffice_fails_honestly(self, client, monkeypatch):
        from app.services.parsers import docx as docx_module

        monkeypatch.setattr(docx_module, "find_soffice", lambda: None)
        resp = client.post("/knowledge/upload", files={"file": ("老旧.doc", b"fake-doc-bytes", "application/msword")})
        data = resp.json()["data"]
        assert data["status"] == "failed"
        assert "LibreOffice" in (data["error"] or "")


class TestPdfOutline:
    def test_font_size_outline_extracted(self, client):
        import pymupdf

        pdf = pymupdf.open()
        page = pdf.new_page()
        y = 60
        # 内置 CJK 字体（默认 helv 无中文字形，会渲染为点）
        page.insert_text((72, y), "第一章 电池安全", fontsize=20, fontname="china-s")
        page.insert_text((72, y + 40), "1.1 充电温度", fontsize=14, fontname="china-s")
        body = "电池充电温度不得超过 45 度。这是正文内容，用于验证字号法大纲提取的准确性。" * 3
        page.insert_textbox(pymupdf.Rect(72, y + 70, 520, y + 200), body, fontsize=10, fontname="china-s")
        pdf_bytes = pdf.tobytes()
        pdf.close()

        resp = client.post("/knowledge/upload", files={"file": ("安全.pdf", pdf_bytes, "application/pdf")})
        assert resp.json()["data"]["status"] == "indexed"
        preview = client.get("/knowledge/preview/安全").json()["data"]
        titles = [o["title"] for o in preview["outline"]]
        assert any("第一章" in t for t in titles), f"应提取到大纲: {titles}"
        # 大纲分级：大字号级别更小
        levels = {o["title"]: o["level"] for o in preview["outline"]}
        first = next(v for k, v in levels.items() if "第一章" in k)
        sub = next((v for k, v in levels.items() if "1.1" in k), None)
        if sub is not None:
            assert first < sub
