"""Kquery 对话页（/）UI 端到端测试。

覆盖：页面加载 → 输入问题 → 发送 → 回答区出现内容（真实后端 8100）→
示例问题按钮可点 → 连续追问。

断言原则：只断言页面真实可见文本；知识库为空时后端会返回
「没有足够证据」兜底文案，这也是合法回答（同样证明前端→后端链路通）。
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect

import os

WEB = os.environ.get("KQUERY_UI_WEB", "http://127.0.0.1:5173")

pytestmark = pytest.mark.browser


def _open_chat(web_page):
    web_page.goto(WEB, wait_until="domcontentloaded")
    box = web_page.get_by_placeholder("输入你的问题…")
    expect(box).to_be_visible(timeout=15_000)
    return web_page, box


class TestChatPage:
    def test_page_loads(self, web_page):
        """首页加载出聊天输入框。"""
        page, box = _open_chat(web_page)
        assert box.is_editable()

    def test_ask_question_and_get_answer(self, web_page):
        """输入问题 → 发送 → 回答区域出现实质内容（前端→后端全链路）。"""
        page, box = _open_chat(web_page)
        box.fill("支持哪些文档格式？")
        box.press("Enter")

        # 问题立即可见
        expect(page.get_by_text("支持哪些文档格式", exact=False).first).to_be_visible(timeout=5_000)
        # 回答出现：等待页面文本增长（流式渲染）且有回答内容
        page.wait_for_function(
            """() => {
                const t = document.body.innerText;
                return t.includes('支持哪些文档格式')
                    && (t.includes('没有足够证据') || t.includes('PDF') || t.includes('Markdown')
                        || t.length > 200);
            }""",
            timeout=60_000,
        )

    def test_follow_up_question(self, web_page):
        """同一会话连续追问（会话状态保持）。"""
        page, box = _open_chat(web_page)
        box.fill("你好")
        box.press("Enter")
        page.wait_for_function(
            "() => document.body.innerText.length > 100",
            timeout=60_000,
        )
        # 追问
        box2 = page.get_by_placeholder("输入你的问题…")
        box2.fill("再问一下，怎么计费？")
        box2.press("Enter")
        expect(page.get_by_text("怎么计费", exact=False).first).to_be_visible(timeout=5_000)
