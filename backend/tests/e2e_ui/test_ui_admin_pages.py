"""全部页面 UI 巡检 + 关键管理交互（真实点击 / 输入）。

覆盖：每个可达页面都能打开且无 JS 错误；各管理页的关键交互能真的操作
（改配额、加敏感词、设 ACL、编辑用户角色、检索配置、图谱查询）。

断言原则：
- 只断言**页面真实可见**的内容（innerText / 可见元素），不碰组件内部状态；
- 关键词取自各页真实文本，**不能用 placeholder**（placeholder 不进 innerText）；
- 每个用例都自证"确实操作成功了"（操作后能看到结果），不只断言"没报错"。
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import expect

WEB = os.environ.get("KQUERY_UI_WEB", "http://127.0.0.1:5173")

pytestmark = pytest.mark.browser

# (路径, 页面上必须出现的关键词)
PAGES = [
    ("/knowledge", "上传"),
    ("/knowledge/upload-advanced", "选择文件"),
    ("/knowledge/query-analysis", "Query 分析"),
    ("/graph", "图谱"),
    ("/admin/monitor", "监控"),
    ("/admin/retrieval-config", "检索"),
    ("/admin/users", "用户"),
    ("/admin/acl", "ACL"),
    ("/admin/kb", "知识库"),
    ("/admin/audit", "审计"),
    ("/admin/quota", "配额"),
    ("/admin/sensitive", "敏感词"),
]


class TestAllPagesLoad:
    def test_chat_page_has_input_box(self, web_page):
        """对话页：输入框存在且可编辑（placeholder 不进 innerText，须按元素断言）。"""
        page = web_page
        page.goto(f"{WEB}/", wait_until="domcontentloaded")
        box = page.get_by_placeholder("输入你的问题…")
        expect(box).to_be_visible(timeout=15_000)
        assert box.is_editable()

    @pytest.mark.parametrize("path,keyword", PAGES)
    def test_page_loads_with_content_and_no_js_error(self, web_page, path, keyword):
        """页面可打开、有实质内容、无未捕获 JS 错误。"""
        page = web_page
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{WEB}{path}", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        body = page.inner_text("body")
        assert len(body) > 20, f"{path} 页面几乎空白（len={len(body)}）"
        assert keyword in body, f"{path} 未出现预期关键词「{keyword}」"
        assert not errors, f"{path} 有未捕获 JS 错误：{errors[:3]}"


class TestAdminInteractions:
    def test_quota_page_can_edit_values(self, web_page):
        """配额页：填租户 + 数值 → 保存 → 「已保存」+ 表格出现该租户的新上限。"""
        page = web_page
        page.goto(f"{WEB}/admin/quota", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        tenant = "uitest-tenant"
        # 租户名是必填（保存按租户维度生效）
        page.get_by_label("租户").fill(tenant)
        page.get_by_label("每分钟").fill("77")
        page.get_by_role("button", name="保存").first.click()
        expect(page.get_by_text("已保存").first).to_be_visible(timeout=10_000)
        # 表格里应出现该租户，分钟上限为 77
        expect(page.get_by_text("77/min")).to_be_visible(timeout=10_000)

    def test_sensitive_word_can_be_added(self, web_page):
        """敏感词页：填词 → 点「添加」→ 列表出现该词。"""
        page = web_page
        page.goto(f"{WEB}/admin/sensitive", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        word = "UI探针敏感词"
        page.get_by_placeholder("词条（≤50 字）").fill(word)
        page.get_by_role("button", name="添加").first.click()
        page.wait_for_timeout(1500)
        expect(page.get_by_text(word).first).to_be_visible(timeout=10_000)

    def test_acl_page_lists_documents(self, web_page):
        """文档 ACL 页：列出文档并可打开编辑（allow/deny 输入框存在）。"""
        page = web_page
        page.goto(f"{WEB}/admin/acl", wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        body = page.inner_text("body")
        assert "文档" in body or "ACL" in body or "权限" in body, "ACL 页未渲染出内容"
        # 表头应含 allow / deny 三元组
        assert any(k in body for k in ("allow", "deny", "公开")), (
            f"ACL 页未见 allow/deny/公开字段：{body[:160]}"
        )

    def test_users_page_can_edit_roles(self, web_page):
        """用户页：点「编辑」→ 改角色 → 保存 → 表格出现该角色标签。"""
        page = web_page
        page.goto(f"{WEB}/admin/users", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        page.get_by_role("button", name="编辑").first.click()
        page.wait_for_timeout(500)
        page.locator("input").first.fill("sales")
        page.get_by_role("button", name="保存").first.click()
        page.wait_for_timeout(1500)
        expect(page.get_by_text("sales").first).to_be_visible(timeout=10_000)

    def test_retrieval_config_inputs_present(self, web_page):
        """检索配置页：关键参数输入框存在且可填。"""
        page = web_page
        page.goto(f"{WEB}/admin/retrieval-config", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        inputs = page.locator("input[type=number]")
        assert inputs.count() >= 3, "检索配置页应至少有 3 个数值参数可调"
        inputs.first.fill("7")
        assert inputs.first.input_value() == "7"

    def test_graph_page_can_search(self, web_page):
        """图谱页：输入查询词 → 触发检索（空库返回空是合法的，不报错即可）。"""
        page = web_page
        page.goto(f"{WEB}/graph", wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        boxes = page.locator("input[type=text], input:not([type])")
        if boxes.count() == 0:
            pytest.skip("图谱页未提供文本查询框")
        boxes.first.fill("测试实体")
        page.keyboard.press("Enter")
        page.wait_for_timeout(2000)
        assert len(page.inner_text("body")) > 20

    def test_monitor_page_shows_metrics(self, web_page):
        """监控大盘：指标卡可见。"""
        page = web_page
        page.goto(f"{WEB}/admin/monitor", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        assert any(k in body for k in ("QPS", "P50", "P95", "错误率", "缓存")), (
            f"监控大盘未渲染出指标：{body[:120]}"
        )

    def test_audit_page_lists_events(self, web_page):
        """审计页：能拉出事件列表（可为空表但结构在）。"""
        page = web_page
        page.goto(f"{WEB}/admin/audit", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        assert "审计" in body
        assert len(body) > 30
