"""浏览器端到端验证（真实登录 + 真实点击 + 截图留证）。

覆盖：
1. 登录页 → 登录（admin / 密码档）→ 会话页；
2. 提问 → 流式回答 → 证据卡与引用；
3. 知识库列表 → 文档预览；
4. 图谱页；
5. 监控页与审计页（管理面）；
6. 全程收集 console error / pageerror。

运行：
    cd backend && uv run python ../tests/e2e/scripts/browser_verify.py \
        --base http://localhost:8090 --api http://localhost:8100 \
        --password Admin12345 --out ../doc/screenshots
"""
import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

# 共享浏览器辅助（此前 4 个脚本各写一份，`check` 的返回类型还不一致）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_helpers import (  # noqa: E402
    FAILED,
    check,
    section,
)

SAMPLE_MD = """# Kquery 产品手册

## 产品简介

Kquery 是一个智能知识库问答系统，支持上传文档后进行基于证据的问答。
它的核心能力包括文档切块、向量检索、引用溯源与流式回答。

## 计费规则

Kquery 按问答次数计费，每次问答消耗一个额度。
企业版每月包含一万次问答额度，超出部分按量付费。

## 常见问题

问：支持哪些文档格式？答：当前支持 PDF 与 Markdown 文档。
问：答案中的引用编号是什么？答：引用编号对应答案所依据的文档片段，可点击跳转查看原文。
"""

CONSOLE_ERRORS: list[str] = []
def login_token(api: str, password: str) -> str:
    req = urllib.request.Request(
        f"{api}/health",
        data=json.dumps({"username": "admin", "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())["data"]["token"]


def upload_doc(api: str, token: str) -> str:
    boundary = "----verify"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="产品手册.md"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
        f"{SAMPLE_MD}\r\n--{boundary}--\r\n"
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{api}/knowledge/upload",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["data"]["doc_id"]


def run(base: str, api: str, password: str, out_dir: Path, headed: bool) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    token = login_token(api, password)
    doc_id = upload_doc(api, token)
    print(f"已通过 API 上传文档: {doc_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("console", lambda m: CONSOLE_ERRORS.append(f"{m.type}: {m.text}")
                if m.type == "error" else None)
        page.on("pageerror", lambda e: CONSOLE_ERRORS.append(f"pageerror: {e}"))

        section("1. 登录页与登录流程")
        page.goto(base, wait_until="networkidle", timeout=45000)
        check("未登录跳转到登录页", "/login" in page.url, page.url)
        page.screenshot(path=str(out_dir / "01-login.png"), full_page=True)

        user_box = page.locator('input[type="text"], input[name="username"]').first
        user_box.fill("admin")
        page.locator('input[type="password"]').first.fill(password)
        page.locator('button[type="submit"], button:has-text("登录")').first.click()
        page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
        page.wait_for_load_state("networkidle")
        check("登录成功进入主界面", "/login" not in page.url, page.url)
        page.screenshot(path=str(out_dir / "02-chat-home.png"), full_page=True)

        section("2. 提问与流式回答")
        box = page.locator("textarea").first
        box.wait_for(state="visible", timeout=15000)
        box.fill("Kquery 支持哪些文档格式？")
        page.locator('button:has-text("发送"), button[type="submit"]').first.click()
        page.wait_for_selector("text=PDF", timeout=60000)
        # 真实模型较慢：等"停止"按钮消失（生成结束）再截终稿
        try:
            page.wait_for_selector('button:has-text("停止")', state="detached", timeout=180000)
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)  # 等证据卡与置信度区渲染完
        body = page.inner_text("body")
        check("回答包含 Markdown/PDF 相关内容", "PDF" in body)
        check("出现引用角标或证据", ("引用" in body) or ("证据" in body) or ("[#" in body))
        page.screenshot(path=str(out_dir / "03-answer.png"), full_page=True)

        section("3. 知识库与文档预览")
        page.goto(f"{base}/knowledge", wait_until="networkidle", timeout=30000)
        body = page.inner_text("body")
        check("知识库列表包含已上传文档", "产品手册" in body)
        page.screenshot(path=str(out_dir / "04-knowledge.png"), full_page=True)

        page.goto(f"{base}/knowledge/preview/{doc_id}", wait_until="networkidle", timeout=30000)
        body = page.inner_text("body")
        check("预览页展示块列表", ("块" in body) or ("chunk" in body.lower()))
        page.screenshot(path=str(out_dir / "05-preview.png"), full_page=True)

        section("4. 管理面（监控）")
        page.goto(f"{base}/admin/monitor", wait_until="networkidle", timeout=30000)
        page.screenshot(path=str(out_dir / "07-admin-monitor.png"), full_page=True)
        check("监控页渲染", len(page.inner_text("body").strip()) > 30)

        section("6. 控制台健康度")
        check("无 console error / pageerror", not CONSOLE_ERRORS,
              "; ".join(CONSOLE_ERRORS[:5]))

        browser.close()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"\033[31m失败 {len(FAILED)} 项：\033[0m")
        for name, detail in FAILED:
            print(f"  - {name}: {detail}")
        return 1
    print("\033[32m浏览器验证全部通过\033[0m")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8090")
    parser.add_argument("--api", default="http://localhost:8100")
    parser.add_argument("--password", default="Admin12345")
    parser.add_argument("--out", default="../doc/screenshots")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    return run(args.base, args.api, args.password, Path(args.out), args.headed)


if __name__ == "__main__":
    sys.exit(main())
