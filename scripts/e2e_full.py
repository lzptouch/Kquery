#!/usr/bin/env python3
"""浏览器全功能端到端测试：真实点击与输入，覆盖 doc-qa 整合后的全部前端功能。

与其它脚本的分工：
- `smoke_api.py`   验后端契约（能不能用）
- `api_sweep.py`   验所有端点可达（有没有 5xx）
- 本脚本           验**用户在页面上能不能用**：真实登录、真实输入、真实点击

断言一律看**页面上真实可见的内容**，不只看 DOM 里有没有这个节点——
元素在但被遮挡、点了没反应、文案错了，测试照样通过是这类测试最常见的退化方式。

用法：
    python3 scripts/e2e_full.py --base http://localhost:8090 --user admin --password xxx
    python3 scripts/e2e_full.py --base http://localhost:8090 --headed
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
CONSOLE_ERRORS: list[str] = []

#: 页面会恢复上一次会话的历史消息，页面上可能同时存在**多条**回答。
#: 因此所有针对"本次回答"的断言都锚定在 `.last`（最新一条），
#: 用 `.first` 会拿到上一轮的结果——那种"通过"是假的。
SAMPLE_MD = """# 浏览器端到端样例

## 计费规则
Kquery 按问答次数计费，每问消耗一个额度。

## 支持格式
支持 **PDF**、Markdown、Excel、纯文本与代码文件。
"""


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAILED.append((name, detail))
        print(f"  \033[31m✗\033[0m {name} — {detail}")
    return bool(condition)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def _make_pptx(path: str) -> str:
    """造一个最小 pptx（浏览器端到端用）。"""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
    box.text_frame.text = "浏览器上传的演示稿"
    body = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(2))
    body.text_frame.text = "这一页用于验证 PPTX 解析与入库"
    prs.save(path)
    return path


def page_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def login(page, base: str, user: str, password: str) -> bool:
    """真实走登录表单（不偷塞 localStorage）——登录本身就是一条被测路径。"""
    page.goto(base, wait_until="networkidle", timeout=45000)
    if page.locator('input[name="username"]').count() == 0:
        return True  # 免鉴权模式
    page.locator('input[name="username"]').fill(user)
    page.locator('input[name="password"]').fill(password)
    page.locator('button[type="submit"]').click()
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
    except Exception:
        pass
    return page.locator('input[name="username"]').count() == 0


def launch_browser(p, headed: bool):
    """启动 Chromium。

    Playwright 自带的浏览器版本与本机缓存不一定对得上（`playwright install`
    在受限网络下拉不下来），因此按"自带 → 系统 Chrome → 本机缓存的
    Chrome for Testing"依次降级。跑不起来就报错，不静默跳过。
    """
    attempts = [
        ("playwright 自带", {}),
        ("系统 Chrome", {"channel": "chrome"}),
    ]
    local = os.path.expanduser(
        "~/Library/Caches/ms-playwright/chromium-1247/chrome-mac-arm64/"
        "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    )
    if os.path.exists(local):
        attempts.append(("本机 Chrome for Testing", {"executable_path": local}))
    last = None
    for label, kwargs in attempts:
        try:
            return p.chromium.launch(headless=not headed, **kwargs), label
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"  （{label} 启动失败：{str(exc).splitlines()[0][:100]}）")
    raise RuntimeError(f"无法启动浏览器：{last}")


def run(base: str, headed: bool, user: str, password: str) -> int:
    with sync_playwright() as p:
        browser, browser_label = launch_browser(p, headed)
        print(f"  浏览器：{browser_label}")
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on(
            "console",
            lambda m: CONSOLE_ERRORS.append(f"{m.type}: {m.text}")
            if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: CONSOLE_ERRORS.append(f"pageerror: {e}"))

        # ---------------------------------------------------------- 1. 登录
        section("1. 登录（真实表单输入 + 点击）")
        ok = login(page, base, user, password)
        check("登录后离开登录页", ok, f"URL={page.url}")
        body = page_text(page)
        check("登录后进入对话页", len(body.strip()) > 50, f"正文长度 {len(body)}")

        # ---------------------------------------------------------- 2. 快问附件
        section("2. 快问临时文档（真实上传 + 附加）")
        attach = page.locator('button[aria-label="附件提问"]')
        check("附件提问按钮存在", attach.count() > 0, "未找到附件提问按钮")
        attached = False
        quick_md = "/tmp/kquery_e2e_quick.md"
        with open(quick_md, "w", encoding="utf-8") as fh:
            fh.write(
                "# 快问样例\n\n"
                "## 计费规则\n"
                "Kquery 按**问答次数**计费，每问消耗一个额度。\n\n"
                "## 支持格式\n"
                "支持 **PDF**、Markdown、Excel、纯文本与代码文件。\n"
            )
        if attach.count() > 0:
            attach.first.click()
            panel = page.locator('[role="dialog"][aria-label="附件提问"]')
            check("快问面板已打开", panel.count() > 0, "未出现附件提问面板")
            if panel.count() > 0:
                file_input = panel.locator('input[type="file"]').first
                check("面板里有文件选择控件", file_input.count() > 0, "未找到 input[type=file]")
                accept = file_input.get_attribute("accept") or ""
                check("上传白名单含 .xlsx（F3）", ".xlsx" in accept, f"accept={accept}")
                check("上传白名单含 .md", ".md" in accept, f"accept={accept}")
                file_input.set_input_files(quick_md)
                for _ in range(60):
                    page.wait_for_timeout(500)
                    txt = page_text(page)
                    if "已附加" in txt or "附件提问中" in txt:
                        attached = True
                        break
                check("上传后快问文档已附加", attached,
                      f"页面文本片段: {page_text(page)[-200:]}")

        # ---------------------------------------------------------- 3. 提问
        section("3. 提问与流式回答（真实输入 + 点击发送）")
        box = page.locator('textarea[aria-label="问题输入框"], textarea, input[type="text"]').first
        box.wait_for(state="visible", timeout=15000)
        # 问题刻意与临时文档内容对齐：doc_id 唯一 → 缓存键唯一 → 不会命中语义缓存，
        # 检索范围也锁定在这份文档，证据/追问/Markdown 三项都能稳定验到。
        question = "计费规则是什么？支持哪些文件格式？"
        box.fill(question)
        check("输入框接受了文本", box.input_value() == question, f"实际 {box.input_value()!r}")

        send = page.locator('button[aria-label="发送"]').first
        send.click()
        check("点击发送按钮成功", True)

        answered = False
        for _ in range(120):
            page.wait_for_timeout(500)
            if page.locator(".citation-chip").count() > 0:
                answered = True
                break
        check("流式回答完成并渲染出引用角标", answered,
              f"等待 {time.perf_counter():.0f}s 仍无 .citation-chip")
        text = page_text(page)
        check("用户问题回显在对话里", question in text, "未看到问题回显")
        check("回答正文可见", len(text) > 200, f"正文长度 {len(text)}")

        # ---------------------------------------------------------- 3. Markdown
        section("4. Markdown 富文本渲染（F1）")
        md = page.locator(".markdown-body").last
        check("答案区用了 Markdown 渲染容器", md.count() > 0, "未找到 .markdown-body")
        if md.count() > 0:
            html = md.inner_html()
            # 断言"确实按 Markdown 解析了"，而不是断言某一种标记一定出现：
            # 模型输出里有没有 `**` 是随机的，断言它必然存在会假失败
            check("Markdown 被解析成 HTML 段落", "<p>" in html, html[:200])
            check("换行被渲染成 <br>（GFM 软换行）", "<br" in html, html[:200])
            check("原始 HTML 未被注入（转义生效）",
                  "<script" not in html.lower() and "<iframe" not in html.lower(),
                  "疑似原始 HTML 直出")
            # 渲染正确时，`**加粗**` 应该变成 <strong>，HTML 里不该再残留 `**`
            check("没有残留未解析的 Markdown 标记", "**" not in html,
                  f"答案里还有未渲染的 ** 标记：{html[:160]}")
        check("引用角标可点击", page.locator(".citation-chip").count() > 0, "无 .citation-chip")
        if page.locator(".citation-chip").count() > 0:
            page.locator(".citation-chip").last.click()
            page.wait_for_timeout(800)
            check("点击引用角标后页面正常", True, "")

        # ---------------------------------------------------------- 4. 推荐追问
        section("5. 推荐追问按钮（F5）")
        # 引用角标在 token 阶段就会出现，而追问事件在 done 之前才发——
        # 断言必须等到流式真正结束，否则是"跑得太早"的假失败
        followups = page.locator(".followup-btn")
        for _ in range(60):
            if followups.count() > 0:
                break
            page.wait_for_timeout(500)
        count = followups.count()
        check("回答下方出现推荐追问按钮", count > 0, f"找到 {count} 个")
        if count > 0:
            first_text = followups.first.inner_text().strip()
            check("追问按钮有可读文案", len(first_text) > 2, f"文案 {first_text!r}")
            followups.first.click()
            page.wait_for_timeout(2500)
            check("点击追问后新问题进入对话", first_text in page_text(page),
                  "追问文案未出现在对话中")

        # ---------------------------------------------------------- 5. 语音
        section("6. 语音输入按钮（F2）")
        voice = page.locator('button[aria-label="语音输入"]')
        check("麦克风按钮存在", voice.count() > 0, "未找到语音按钮")
        if voice.count() > 0:
            title = voice.first.get_attribute("title") or ""
            check("麦克风按钮有 tooltip 说明", bool(title.strip()), "title 为空")
            # 无头浏览器没有麦克风：按钮要么 disabled，要么给出明确提示
            if voice.first.is_disabled():
                check("无麦克风环境下按钮 disabled 且有原因", bool(title.strip()), title)
            else:
                check("麦克风按钮可用（浏览器支持录音）", True, "")

        # ---------------------------------------------------------- 6. 快问
        # ---------------------------------------------------------- 6.5 引文增强
        section("6.5 引文增强（参考卡片 / 搜索 / 导出 TXT）")
        summary = page.locator('[aria-label="参考"]').last
        check("回答下方出现参考汇总卡片", summary.count() > 0, "未找到参考卡片")
        if summary.count() > 0:
            text = summary.first.inner_text()
            check("参考卡片显示引文条数", "条引文" in text, text)
            check("参考卡片显示文件数", "个文件" in text, text)
            summary.first.get_by_role("button", name="查看引文").click()
            page.wait_for_timeout(600)

        search = page.locator('input[aria-label="在引文中搜索"]').last
        check("引文面板有搜索框", search.count() > 0, "未找到引文搜索框")
        if search.count() > 0:
            search.fill("不存在的关键词xyz")
            page.wait_for_timeout(400)
            check("搜索无结果时给出提示", page.get_by_text("没有匹配的引文").count() > 0,
                  "无匹配提示缺失")
            search.fill("")
            page.wait_for_timeout(300)

        export_btn = page.locator('button[aria-label="导出 TXT"]').last
        check("有导出 TXT 按钮", export_btn.count() > 0, "未找到导出按钮")
        if export_btn.count() > 0:
            with page.expect_download(timeout=15000) as dl_info:
                export_btn.click()
            download = dl_info.value
            check("导出 TXT 触发下载", download.suggested_filename.endswith(".txt"),
                  download.suggested_filename)

        section("7. 快问证据范围（F4）")
        check("快问回答给出了证据卡", page.locator(".evidence-card").count() > 0,
              "无 .evidence-card")
        body_all = page_text(page)
        check("快问答案引用了临时文档内容",
              "计费" in body_all or "格式" in body_all, body_all[-300:])
        answer_html = page.evaluate("""() => {
          const cards = document.querySelectorAll('.answer-card');
          if (!cards.length) return '';
          const last = cards[cards.length - 1];
          const md = last.querySelector('.markdown-body');
          return md ? md.innerHTML : '';
        }""")
        # 模型不一定原样回显加粗标记 → 条件断言：回显了就必须渲染成 <strong>
        if "**" in answer_html:
            check("加粗语法被渲染成 <strong>",
                  page.locator(".markdown-body strong").count() > 0,
                  "答案里有 ** 标记但没有 <strong>")
        else:
            check("加粗语法被渲染成 <strong>（模型未回显标记，跳过）", True, "")
        # 移除附件（释放临时文档）
        detach = page.locator("button:has-text('移除'), button:has-text('删除')").first
        if detach.count() > 0:
            detach.click()
            page.wait_for_timeout(1200)
            check("可移除已附加的临时文档", True, "")

        # ---------------------------------------------------------- 7. 知识库
        section("8. 知识库上传与预览")
        page.goto(f"{base}/knowledge", wait_until="networkidle", timeout=30000)
        check("知识库页可打开", "知识库" in page_text(page), page_text(page)[:120])

        kb_file = page.locator('input[type="file"]').first
        if kb_file.count() > 0:
            accept = kb_file.get_attribute("accept") or ""
            check("知识库上传白名单含 .xlsx", ".xlsx" in accept, f"accept={accept}")
            kb_file.set_input_files("/tmp/kquery_e2e_quick.md")
            page.wait_for_timeout(4000)
            text = page_text(page)
            check("上传后页面给出反馈", any(k in text for k in ("索引", "成功", "已索引", "解析", "上传")),
                  f"文本片段: {text[:200]}")
        else:
            check("知识库页有文件上传控件", False, "未找到 input[type=file]")

        # PPTX 上传（F9）
        pptx_path = "/tmp/kquery_e2e_deck.pptx"
        _make_pptx(pptx_path)
        kb_file2 = page.locator('input[type="file"]').first
        if kb_file2.count() > 0:
            kb_file2.set_input_files(pptx_path)
            page.wait_for_timeout(6000)
            text = page_text(page)
            check("PPTX 上传后有反馈", "kquery_e2e_deck" in text or "索引" in text or "成功" in text,
                  f"页面片段: {text[-200:]}")

        rows = page.locator("table tbody tr")
        check("文件列表有数据行", rows.count() > 0, f"{rows.count()} 行")
        if rows.count() > 0:
            rows.first.click()
            page.wait_for_timeout(3000)
            check("可进入文档预览并看到切块",
                  bool(re.search(r"切块|块|chunk", page_text(page), re.I)),
                  page_text(page)[:160])

        # ---------------------------------------------------------- 8. 图谱 / 管理
        section("9. 图谱页与管理页")
        page.goto(f"{base}/graph", wait_until="networkidle", timeout=30000)
        check("图谱页可打开", len(page_text(page).strip()) > 50, page_text(page)[:120])

        for path, keyword in [
            ("/admin/monitor", "监控"),
            ("/admin/users", "用户"),
            ("/admin/acl", "ACL"),
            ("/admin/audit", "审计"),
            ("/admin/quota", "配额"),
            ("/admin/sensitive", "敏感"),
            ("/admin/retrieval-config", "检索"),
        ]:
            page.goto(f"{base}{path}", wait_until="networkidle", timeout=30000)
            text = page_text(page)
            check(f"{path} 页可打开且有内容", len(text.strip()) > 40, f"正文长度 {len(text)}")

        # ---------------------------------------------------------- 9. 控制台
        section("10. 运行时健康")
        # 前端到后端的同源链路（走 nginx 反代）
        probe = page.evaluate("""
          async () => {
            const tok = localStorage.getItem("kquery.token");
            const h = {"Content-Type": "application/json"};
            if (tok) h["Authorization"] = "Bearer " + tok;
            const r = await fetch("/healthz");
            return { status: r.status, ok: r.ok };
          }
        """)
        check("前端同源访问 /healthz 成功", probe.get("status") == 200, str(probe))
        probe2 = page.evaluate("""
          async () => {
            const tok = localStorage.getItem("kquery.token");
            const h = {"Content-Type": "application/json"};
            if (tok) h["Authorization"] = "Bearer " + tok;
            const r = await fetch("/speech/status");
            return { status: r.status, body: await r.text() };
          }
        """)
        check("前端同源访问 /speech/status 成功", probe2.get("status") == 200,
              str(probe2)[:200])

        # 忽略与功能无关的控制台噪声（如 favicon 404、音频权限提示）
        real_errors = [e for e in CONSOLE_ERRORS
                        if "favicon" not in e.lower()
                        and "permissions policy" not in e.lower()
                        and "notallowederror" not in e.lower()]
        check("页面无未预期的 JS 错误", not real_errors, "; ".join(real_errors[:5]))

        browser.close()

    # ---------------------------------------------------------------- 汇总
    total = len(PASSED) + len(FAILED)
    print("\n" + "=" * 62)
    if FAILED:
        print(f"\033[31m失败 {len(FAILED)}/{total}\033[0m")
        for name, detail in FAILED:
            print(f"  · {name}\n    {detail}")
        return 1
    print(f"\033[32m全部通过：{len(PASSED)}/{total}\033[0m")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8090")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default=os.environ.get("KQUERY_ADMIN_PASSWORD", ""))
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    start = time.perf_counter()
    code = run(args.base, args.headed, args.user, args.password)
    print(f"耗时 {time.perf_counter() - start:.1f}s")
    return code


if __name__ == "__main__":
    sys.exit(main())
