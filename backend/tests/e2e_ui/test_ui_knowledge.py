"""Kquery 知识库页（/knowledge）UI 端到端测试。

覆盖：知识库页加载 → 上传 Markdown 文档 → 列表出现该文档 →
回到对话页用上传内容问答（上传→索引→检索→回答 全链路）。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

WEB = "http://127.0.0.1:5173"

pytestmark = pytest.mark.browser

SAMPLE_MD = """# UI 测试专用文档

## 测试产品退货政策

UITEST 产品支持三十天无理由退货，自签收之日起计算。
退货时需保持商品包装完好，并附退货申请单。
"""


class TestKnowledgePage:
    def test_knowledge_page_loads(self, web_page):
        """知识库页可打开且非空白。"""
        web_page.goto(f"{WEB}/knowledge", wait_until="domcontentloaded")
        web_page.wait_for_timeout(3_000)
        body = web_page.inner_text("body")
        assert len(body) > 10  # 页面有实质内容

    def test_upload_document_via_browser(self, web_page):
        """通过文件选择器上传文档，列表中出现该文档。"""
        page = web_page
        page.goto(f"{WEB}/knowledge", wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", prefix="uitest_", delete=False, encoding="utf-8"
        ) as f:
            f.write(SAMPLE_MD)
            tmp = f.name

        try:
            # 找文件上传入口：input[type=file]（可能隐藏，用 set_input_files 直传）
            file_input = page.locator("input[type='file']").first
            file_input.set_input_files(tmp)

            # 等待上传完成：页面出现文件名或「三十天无理由」相关内容
            page.wait_for_function(
                """() => {
                    const t = document.body.innerText;
                    return t.includes('uitest_') || t.includes('UI 测试');
                }""",
                timeout=30_000,
            )
        finally:
            Path(tmp).unlink(missing_ok=True)
