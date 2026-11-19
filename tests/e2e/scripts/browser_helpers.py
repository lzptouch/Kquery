"""浏览器端到端脚本的共用辅助（page_text / login + 控制台报告）。

**为什么抽出来**：此前 `browser_verify.py` / `e2e_browser.py` /
`e2e_new_features.py` / `e2e_full.py` 各写一份同名函数，而且
**行为并不一致**——`check` 有的返回 bool、有的返回 None，
失败记录的形状也不同（`(name, detail)` 元组 vs 拼好的字符串）。
调用方"靠返回值决定要不要记失败"的写法因此也跟着不一致。

现在：控制台报告在 `console.py`（HTTP 脚本也用它），这里只放浏览器特有的部分。
"""
from __future__ import annotations

from console import FAILED, PASSED, check, reset, section, summary  # noqa: F401

__all__ = ["FAILED", "PASSED", "check", "login", "page_text", "reset", "section", "summary"]


def page_text(page) -> str:
    """取页面文本（失败返回空串，不抛）。"""
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


def login(page, base: str, user: str, password: str) -> tuple[bool, bool]:
    """真实走登录表单（不偷塞 localStorage）——登录本身就是一条被测路径。

    没有登录这一步的话，脚本只能打免鉴权的开发形态（vite dev server +
    无需登录）。部署形态首屏直接是聊天页，
    页面上根本没有对话输入框，第一个断言就会以"元素找不到"超时收场。

    返回 ``(是否登录成功, 是否口令档)``。
    """
    page.goto(base, wait_until="networkidle", timeout=45000)
    if page.locator('input[name="username"]').count() == 0:
        return True, False  # 免鉴权模式
    page.locator('input[name="username"]').fill(user)
    page.locator('input[name="password"]').fill(password)
    page.locator('button[type="submit"]').click()
    try:
        page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
    except Exception:  # noqa: BLE001
        pass
    return page.locator('input[name="username"]').count() == 0, True
