"""知识库管理页（/admin/kb）UI 端到端测试：真实点击 / 输入 / 勾选。

覆盖：新建 → 授权（勾选用户 + 输入库名二次确认 + 影响人数回显）→
编辑 → 上传时选择目标知识库 → 文档列表显示所属知识库 → 用户页反查可用知识库。

与 tests/e2e（接口契约）的分工：这里证明"人在页面上能用"——
按钮能点、输入框能填、勾选态正确、结果能看见。
"""
from __future__ import annotations

import os
import time

import pytest
from playwright.sync_api import expect

WEB = os.environ.get("KQUERY_UI_WEB", "http://127.0.0.1:5173")

pytestmark = pytest.mark.browser


def _kb_name() -> str:
    """每次运行用唯一名，避免上一轮残留影响断言（真实后端，测试不隔离数据）。"""
    return f"UI测试库{int(time.time()) % 100000}"


def _open_kb_page(web_page):
    web_page.goto(f"{WEB}/admin/kb", wait_until="domcontentloaded")
    expect(web_page.get_by_text("知识库管理").first).to_be_visible(timeout=15_000)
    return web_page


def _row_of(page, name: str):
    return page.locator("tr", has_text=name).first


def _btn(page, name: str, row_name: str):
    """行内按钮：必须按 role 定位——状态标签（如「仅授权」）与按钮（「授权」）文本同形，
    按文本定位会撞 playwright 的 strict mode violation。"""
    return _row_of(page, row_name).get_by_role("button", name=name)


class TestKbManagement:
    def test_page_lists_default_kb(self, web_page):
        """管理页打开即见默认知识库（存量文档的锚点）。"""
        page = _open_kb_page(web_page)
        expect(_row_of(page, "默认知识库")).to_be_visible(timeout=10_000)
        # 默认库不可删（存量文档锚点）
        assert _btn(page, "删除", "默认知识库").count() == 0, (
            "默认知识库不应提供删除入口"
        )

    def test_create_kb_by_clicking_and_typing(self, web_page):
        """点「新建知识库」→ 输入名称 → 点「创建」→ 列表出现该库。"""
        page = _open_kb_page(web_page)
        name = _kb_name()

        page.get_by_text("新建知识库").first.click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=5_000)
        dialog.locator("input.probe-input").first.fill(name)
        dialog.get_by_text("创建").click()

        expect(_row_of(page, name)).to_be_visible(timeout=10_000)
        # 新建库默认「仅授权」（不公开）
        expect(_row_of(page, name).get_by_text("仅授权")).to_be_visible()

    def test_grant_kb_to_user_with_confirm_and_impact(self, web_page):
        """授权全流程：点授权 → 勾选用户 → 输入库名二次确认 → 保存 → 看见影响人数。"""
        page = _open_kb_page(web_page)
        name = _kb_name()

        # 先建库
        page.get_by_text("新建知识库").first.click()
        dialog = page.get_by_role("dialog")
        dialog.locator("input.probe-input").first.fill(name)
        dialog.get_by_text("创建").click()
        expect(_row_of(page, name)).to_be_visible(timeout=10_000)

        # 打开授权弹窗
        _btn(page, "授权", name).click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=5_000)

        # 勾选一个用户（种子用户 lisi）
        lisi = dialog.locator("label", has_text="lisi").first
        lisi.locator("input[type=checkbox]").check()

        # 扩权需二次确认：未输入库名时保存按钮禁用
        save = dialog.get_by_text("保存（显示影响人数）")
        expect(save).to_be_disabled(timeout=5_000)
        dialog.locator("input.probe-input").first.fill(name)
        expect(save).to_be_enabled()

        save.click()
        # 结果回显：影响人数与风险等级
        expect(dialog.get_by_text("新增 1 人可访问", exact=False)).to_be_visible(timeout=10_000)
        expect(dialog.get_by_text("高风险")).to_be_visible()

        # 关闭弹窗后，列表的授权人数应变为 1
        dialog.get_by_text("关闭").click()
        expect(_row_of(page, name).get_by_text("1 人")).to_be_visible(timeout=10_000)

    def test_edit_kb_name(self, web_page):
        """编辑：改名 → 列表显示新名。"""
        page = _open_kb_page(web_page)
        name = _kb_name()

        page.get_by_text("新建知识库").first.click()
        dialog = page.get_by_role("dialog")
        dialog.locator("input.probe-input").first.fill(name)
        dialog.get_by_text("创建").click()
        expect(_row_of(page, name)).to_be_visible(timeout=10_000)

        _btn(page, "编辑", name).click()
        dialog = page.get_by_role("dialog")
        renamed = name + "-改"
        dialog.locator("input.probe-input").first.fill(renamed)
        dialog.get_by_text("保存").click()
        expect(_row_of(page, renamed)).to_be_visible(timeout=10_000)

    def test_upload_with_target_kb_and_doc_list_shows_kb(self, web_page):
        """上传页选目标知识库 → 上传 → 文档列表「所属知识库」列显示该库。"""
        page = _open_kb_page(web_page)
        name = _kb_name()
        page.get_by_text("新建知识库").first.click()
        dialog = page.get_by_role("dialog")
        dialog.locator("input.probe-input").first.fill(name)
        dialog.get_by_text("创建").click()
        expect(_row_of(page, name)).to_be_visible(timeout=10_000)

        # 切到知识库页上传
        page.goto(f"{WEB}/knowledge", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        selector = page.get_by_label("目标知识库")
        expect(selector).to_be_visible(timeout=10_000)
        selector.select_option(label=name)

        with _tmp_md("UI库归属.md") as path:
            page.locator("input[type=file]").first.set_input_files(path)
            # 断言范围必须限定在文档表格内：目标知识库下拉的 <option> 文本也含库名，
            # 用 body.innerText 会立刻为真（option 不可见），造成假通过。
            page.wait_for_function(
                """(name) => {
                    const t = document.querySelector('table.files-table')?.innerText || '';
                    return t.includes(name);
                }""",
                arg=name,
                timeout=30_000,
            )
        # 文档列表「所属知识库」列出现该库名
        table = page.locator("table.files-table")
        expect(table.get_by_text(name).first).to_be_visible(timeout=15_000)

    def test_user_page_shows_accessible_kbs(self, web_page):
        """用户权限页可反查某用户可用的知识库。"""
        page = web_page
        page.goto(f"{WEB}/admin/users", wait_until="domcontentloaded")
        expect(page.get_by_text("用户与权限").first).to_be_visible(timeout=15_000)
        expect(page.get_by_text("可访问知识库")).to_be_visible(timeout=5_000)

        # 点第一行的「查看」，应展示知识库名（至少含默认库）
        page.get_by_text("查看").first.click()
        page.wait_for_timeout(1200)
        text = page.inner_text("body")
        assert "默认知识库" in text, "反查结果应含默认知识库"


def _tmp_md(content: str):
    """临时 md 文件（上下文管理器）。"""
    import tempfile
    from pathlib import Path

    handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="uikb_", delete=False, encoding="utf-8"
    )
    handle.write(content)
    handle.close()
    path = Path(handle.name)

    class _Ctx:
        def __enter__(self):
            return str(path)

        def __exit__(self, *exc):
            # Windows 上浏览器可能仍持有文件句柄 → 删不掉不算失败
            # （临时目录由 OS 回收；测试失败根因不会是这个）
            for _ in range(3):
                try:
                    path.unlink(missing_ok=True)
                    return False
                except PermissionError:
                    time.sleep(0.3)
            return False

    return _Ctx()
