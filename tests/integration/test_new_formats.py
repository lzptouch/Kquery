"""F3/F9 扩展格式的端到端验证：xlsx / pptx / txt / html / 代码文件可上传可检索，
.xls 明确报错。"""
import io

from openpyxl import Workbook


def _pptx_bytes() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text_frame.text = "年度产品路线图"
    body = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(2))
    body.text_frame.text = "第三季度发布星链同步功能"
    table_shape = slide.shapes.add_table(
        2, 2, Inches(1), Inches(4), Inches(6), Inches(1.5)
    )
    table_shape.table.cell(0, 0).text = "季度"
    table_shape.table.cell(0, 1).text = "重点"
    table_shape.table.cell(1, 0).text = "Q3"
    table_shape.table.cell(1, 1).text = "星链同步"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _xlsx_bytes() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "报价"
    ws.append(["产品", "单价", "库存"])
    ws.append(["知识库套餐", "199", "88"])
    ws.append(["企业版套餐", "999", "12"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class TestNewFormats:
    def test_xlsx_upload_and_retrievable(self, client):
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("报价.xlsx", _xlsx_bytes(),
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        data = resp.json()["data"]
        assert data["status"] == "indexed", data

        preview = client.get("/knowledge/preview/报价").json()["data"]
        assert preview["chunks"], "xlsx 应产出块"
        assert preview["tables"], "xlsx 应产出表格记录"
        assert preview["tables"][0]["rows"] == 3
        assert preview["tables"][0]["cols"] == 3
        assert "知识库套餐" in preview["tables"][0]["markdown"]
        # 表格单独成块
        assert any(c["modality"] == "table" for c in preview["chunks"])

        answer = client.post("/ask", json={"question": "知识库套餐单价是多少？"}).json()["data"]
        assert answer["evidence"], "xlsx 内容应可被检索到"
        assert "199" in str(answer["evidence"][0]["content"]) or "199" in answer["answer"]

    def test_pptx_upload_and_retrievable(self, client):
        """F9：PPTX 上传 → 解析（逐页 + 表格）→ 可检索。"""
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("路线图.pptx", _pptx_bytes(),
                            "application/vnd.openxmlformats-officedocument.presentationml.presentation")},
        )
        data = resp.json()["data"]
        assert data["status"] == "indexed", data

        preview = client.get("/knowledge/preview/路线图").json()["data"]
        assert preview["chunks"], "pptx 应产出块"
        assert preview["tables"], "pptx 应产出表格记录"
        assert "季度" in preview["tables"][0]["markdown"]
        assert any(c["modality"] == "table" for c in preview["chunks"]), "表格应单独成块"
        # 预览里块内容是截断的 preview 字段（完整内容在 parents/单块详情里）
        assert any("星链同步" in c["preview"] or "年度产品路线图" in c["preview"]
                   for c in preview["chunks"]), preview["chunks"]

        answer = client.post("/ask", json={"question": "第三季度发布什么功能？"}).json()["data"]
        assert answer["evidence"], "pptx 内容应可被检索到"

    def test_ppt_rejected_with_readable_error(self, client):
        """旧版 .ppt（BIFF/二进制）不是 OOXML，python-pptx 读不了 → 给可读错误。"""
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("老演示.ppt", b"fake-ppt", "application/vnd.ms-powerpoint")},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert "pptx" in body["error"]["message"], body["error"]["message"]

    def test_txt_upload_supported(self, client):
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("说明.txt", "这是一份纯文本说明文件。".encode("utf-8"), "text/plain")},
        )
        assert resp.json()["data"]["status"] == "indexed"

    def test_html_upload_supported(self, client):
        html = "<html><body><h1>标题</h1><p>段落正文内容</p></body></html>".encode("utf-8")
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("page.html", html, "text/html")},
        )
        assert resp.json()["data"]["status"] == "indexed"

    def test_code_file_upload_supported(self, client):
        code = b"def add(a, b):\n    return a + b\n"
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("calc.py", code, "text/x-python")},
        )
        assert resp.json()["data"]["status"] == "indexed"

    def test_xls_rejected_with_readable_error(self, client):
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("老表.xls", b"fake-xls", "application/vnd.ms-excel")},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["status"] == "error"
        assert "xlsx" in body["error"]["message"], body["error"]["message"]

    def test_unsupported_binary_still_rejected(self, client):
        resp = client.post(
            "/knowledge/upload",
            files={"file": ("payload.bin", b"\x00\x01", "application/octet-stream")},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == 2003
