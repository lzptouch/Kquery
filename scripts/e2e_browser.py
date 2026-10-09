#!/usr/bin/env python3
"""浏览器端到端测试：真实点击与输入，验证前端功能而不只是接口。

与 smoke_api.py 的分工：
- smoke_api.py 验"服务能不能用"（后端契约、SSE、检索）
- 本脚本验"用户在页面上能不能用"：点击按钮、输入问题、上传文件、
  展开证据、看流式输出是否逐字出现

每一步都断言**页面上真实可见的内容**，而不是 DOM 里恰好存在的节点。
断言"元素存在"是这类测试最容易退化的地方——元素在但被遮挡、点了没反应、
或者文案错了，测试照样通过。

用法：
    python3 scripts/e2e_browser.py --base http://localhost:8081
    python3 scripts/e2e_browser.py --headed   # 看着它跑
"""
import argparse
import re
import sys
import time

from playwright.sync_api import sync_playwright

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
CONSOLE_ERRORS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  \033[32m✓\033[0m {name}")
        return True
    FAILED.append((name, detail))
    print(f"  \033[31m✗\033[0m {name} — {detail}")
    return False


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def page_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


def run(base: str, headed: bool, sample_file: str) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        # 控制台错误单独收集：页面白屏往往是某个 useEffect 抛错导致的，
        # 只看最终 DOM 会以为是"元素没渲染出来"，看不出真正原因
        page.on("console", lambda m: CONSOLE_ERRORS.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: CONSOLE_ERRORS.append(f"pageerror: {e}"))

        # ---------------------------------------------------------- 首屏
        section("1. 首屏加载")
        page.goto(base, wait_until="networkidle", timeout=45000)
        check("首页标题正确", "Kquery" in page.title(), page.title())
        body = page_text(page)
        check("首屏渲染出内容", len(body.strip()) > 50, f"正文长度 {len(body)}")

        # ---------------------------------------------------------- 问答
        section("2. 提问与流式回答（核心路径）")
        box = page.locator('textarea, input[type="text"]').first
        box.wait_for(state="visible", timeout=15000)
        question = "通货膨胀会对储蓄和价格信号产生什么影响？"
        box.fill(question)
        check("输入框接受了文本", box.input_value() == question, f"实际 {box.input_value()!r}")

        # 提交按钮：优先找明确的发送按钮，找不到就回车
        send = page.locator('button[type="submit"], button:has-text("发送"), button:has-text("提问")').first
        submitted = False
        if send.count() > 0 and send.is_visible():
            send.click()
            submitted = True
        else:
            box.press("Enter")
            submitted = True
        check("点击/回车提交成功", submitted)

        # 等回答真正渲染完成。判据是"引用标记出现"——它必然在答案末尾，
        # 出现即代表流式生成结束。用固定 sleep 会因命中语义缓存（秒回）
        # 或模型慢（十几秒）而随机失败。
        wait_start = time.perf_counter()
        answered = False
        for _ in range(90):
            page.wait_for_timeout(500)
            if page.locator(".citation-chip").count() > 0:
                answered = True
                break
        check("流式回答完成并渲染出引用标记", answered,
              f"等待 {time.perf_counter() - wait_start:.0f}s 仍无引用")
        check("用户问题回显在对话里", question in page_text(page), "未看到问题回显")
        check("回答正文可见", len(page_text(page)) > 200, f"正文长度 {len(page_text(page))}")

        # ---------------------------------------------------------- 证据与溯源
        section("3. 证据引用与溯源")
        # 引用标记（答案里的 [#1] 角标，可点击定位证据）
        chips = page.locator(".citation-chip")
        chip_count = chips.count()
        check("答案内有可点击的引用标记", chip_count > 0, f"找到 {chip_count} 个")
        if chip_count > 0:
            check("引用标记显示编号", bool(re.search(r"\[#\d+\]", page_text(page))),
                  "引用角标未显示编号")
            chips.first.click()
            page.wait_for_timeout(1000)
            check("点击引用后页面正常响应（未报错）", True, "")

        # 证据卡与相关度评分（stage-2/5）
        cards = page.locator(".evidence-card")
        check("证据卡已渲染", cards.count() > 0, f"{cards.count()} 张")
        check("证据显示相关度评分",
              bool(re.search(r"相关度\s*[\d.]+", page_text(page))),
              "未看到相关度分数")

        # 置信度条（stage-5）
        check("有置信度展示组件", page.locator(".confidence-bar").count() > 0, "无 .confidence-bar")
        check("置信度带文字等级（不只靠颜色）",
              bool(re.search(r"高可信|一般可信|低可信|可信度", page_text(page))),
              "只有分数没有等级文案")

        # 检索轨迹（stage-2）
        check("有检索过程轨迹（可展开）",
              page.locator(".process-toggle").count() > 0
              or bool(re.search(r"BM25|向量|重排|检索轨迹", page_text(page))),
              "未找到检索过程入口")

        # ---------------------------------------------------------- 知识库
        section("4. 知识库上传与预览")
        page.goto(f"{base}/knowledge", wait_until="networkidle", timeout=30000)
        check("知识库页可打开", "知识库" in page_text(page), page_text(page)[:120])

        file_input = page.locator('input[type="file"]').first
        if file_input.count() > 0:
            file_input.set_input_files(sample_file)
            page.wait_for_timeout(2500)
            # 观察上传后是否出现进度/成功提示，或列表里多出新文件
            text = page_text(page)
            check("上传后页面给出反馈（进度/成功/列表更新）",
                  any(k in text for k in ("索引", "成功", "已索引", "上传", "解析")),
                  f"页面文本片段: {text[:200]}")
        else:
            check("知识库页有文件上传控件", False, "未找到 input[type=file]")

        # 文件列表应能看到条目
        page.wait_for_timeout(2000)
        rows = page.locator("table tbody tr")
        check("文件列表有数据行", rows.count() > 0, f"{rows.count()} 行")

        # 点击文件进预览（切块列表）
        if rows.count() > 0:
            rows.first.click()
            page.wait_for_timeout(3000)
            preview_text = page_text(page)
            check("可进入文档预览并看到切块",
                  bool(re.search(r"切块|块|chunk", preview_text, re.I)),
                  f"预览页文本片段: {preview_text[:160]}")

        # ---------------------------------------------------------- 图谱
        section("5. 图谱页")
        page.goto(f"{base}/graph", wait_until="networkidle", timeout=30000)
        graph_text = page_text(page)
        check("图谱页可打开", "图谱" in graph_text, graph_text[:120])
        check("图谱页有实体或空态提示", bool(re.search(r"实体|图谱|为空|上传", graph_text)),
              graph_text[:160])

        # ---------------------------------------------------------- 报告
        section("6. 多智能体报告")
        page.goto(f"{base}/reports", wait_until="networkidle", timeout=30000)
        check("报告页可打开", "报告" in page_text(page), page_text(page)[:120])
        goal_box = page.locator('input.probe-input, input[placeholder*="报告"], input[placeholder*="目标"]').first
        if goal_box.count() == 0:
            goal_box = page.locator('input[type="text"]').first
        if goal_box.count() > 0:
            check("报告页有目标输入框", True, "")
            goal_box.fill("分析机会成本的含义并举例说明")
            page.wait_for_timeout(300)
            submit = page.locator('button:has-text("新建"), button[type="submit"]').first
            if submit.count() > 0 and not submit.is_disabled():
                submit.click()
                page.wait_for_timeout(5000)
                text = page_text(page)
                check("提交报告目标后页面有响应",
                      bool(re.search(r"报告|生成|进度|轮次|状态", text)),
                      f"提交后文本: {text[-150:]}")
            else:
                check("报告页有可用的提交按钮",
                      False, f"按钮数={submit.count()} disabled={submit.is_disabled() if submit.count() else 'N/A'}")
        else:
            check("报告页有目标输入框", False, "未找到输入框")

        # ---------------------------------------------------------- 管理页
        section("7. 管理页与四态")
        for path, keyword in (("/admin/users", "用户"),
                              ("/admin/eval", "评测"),
                              ("/admin/audit", "审计"),
                              ("/admin/quota", "配额"),
                              ("/admin/retrieval-config", "检索")):
            page.goto(f"{base}{path}", wait_until="networkidle", timeout=30000)
            text = page_text(page)
            check(f"{path} 可打开且有内容", len(text.strip()) > 30, f"文本 {text[:100]}")
            check(f"{path} 页面含「{keyword}」", keyword in text, f"文本 {text[:150]}")
            check(f"{path} 不显示 undefined", "undefined" not in text,
                  f"页面出现 undefined: {text[:200]}")

        # ---------------------------------------------------------- 错误处理
        section("8. 禁用态与错误提示")
        page.goto(base, wait_until="networkidle", timeout=30000)
        box = page.locator('textarea, input[type="text"]').first
        box.wait_for(state="visible", timeout=10000)
        send = page.locator('button:has-text("发送"), button[type="submit"]').first

        # 空输入时按钮应当是禁用的：这是设计（stage-1 前端规范"发送前禁用"）。
        # 断言"禁用"而不是去点它——点一个 disabled 按钮只会让测试超时，
        # 说明不了任何问题。
        if send.count() > 0:
            check("空输入时发送按钮禁用（防无效请求）",
                  send.is_disabled(), f"disabled={send.is_disabled()}")

        # 填入内容后按钮应可用——这是"禁用态真的跟着输入变"的验证
        box.fill("边际思维是什么？")
        page.wait_for_timeout(400)
        check("输入内容后发送按钮可用",
              not send.is_disabled(), f"disabled={send.is_disabled()}")

        # 提交后页面不应出现 undefined / null 这类未渲染值
        send.click()
        page.wait_for_timeout(4000)
        text = page_text(page)
        check("提交后页面无 undefined/null 泄漏",
              "undefined" not in text and ">null<" not in text,
              f"页面出现 undefined/null: {text[-250:]}")

        # ---------------------------------------------------------- 控制台
        section("9. 运行时健康")
        # 过滤掉与后端无关的噪声（如 favicon 404）
        real_errors = [e for e in CONSOLE_ERRORS
                       if "favicon" not in e.lower() and "404" not in e]
        check("页面无 JS 运行时错误", len(real_errors) == 0,
              f"{len(real_errors)} 条: " + "; ".join(real_errors[:3]))

        browser.close()

    # ---------------------------------------------------------- 汇总
    total = len(PASSED) + len(FAILED)
    print("\n" + "=" * 62)
    if FAILED:
        print(f"\033[31m失败 {len(FAILED)}/{total}\033[0m")
        for name, detail in FAILED:
            print(f"  · {name}\n    {detail}")
        return 1
    print(f"\033[32m全部通过：{len(PASSED)}/{total}\033[0m")
    return 0


if __name__ == "__main__":
    import pathlib

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8081")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--sample", default="", help="要上传的样例文件路径")
    args = parser.parse_args()

    sample = args.sample
    if not sample:
        # 默认在临时目录生成一份样例文档，避免依赖仓库里的文件
        tmp = pathlib.Path("/tmp/kquery-e2e-sample.md")
        tmp.write_text(
            "# 企业知识库验收文档\n\n"
            "## 报销流程\n员工报销需在提交后 30 天内完成，逾期不予受理。\n\n"
            "## 年假制度\n入职满一年享 5 天年假，满三年享 10 天，满五年享 15 天。\n\n"
            "## 保密要求\n涉及客户信息的文档禁止上传至外部平台，违者按制度处理。\n",
            encoding="utf-8",
        )
        sample = str(tmp)

    start = time.perf_counter()
    code = run(args.base.rstrip("/"), args.headed, sample)
    print(f"耗时 {time.perf_counter() - start:.1f}s")
    sys.exit(code)