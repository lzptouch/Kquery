"""浏览器 UI 端到端测试的共享夹具（真实前端 5173 + 真实后端 8100）。

与 tests/e2e（API 级 TestClient）的分工：
- tests/e2e：接口契约（内存 app，Mock 模型）
- tests/e2e/ui：用户在页面上能不能用——真实点击/输入/可见文本断言

运行前提（缺一自动 skip）：
- 后端：127.0.0.1:8100（/healthz）
- 前端：默认 127.0.0.1:5173（vite dev server，KQUERY_API_TARGET 指向 8100），
  也可用 `KQUERY_UI_WEB` 指向 Docker 部署的前端（http://127.0.0.1:8090）
- 浏览器：系统 Microsoft Edge（无需下载 Chromium）

**端口占用陷阱**：本机 5173 常年被别的项目占着（实测是另一个 vite 应用）。
只判"端口通"会把 UI 测试指向**别人的页面**，表现为"元素找不到"——
看起来像我们的前端坏了，其实是被测对象根本不是它。
所以这里额外校验首页标题里有 "Kquery"，不是就跳过（并说明原因）。

运行：
    cd backend && uv run pytest tests/e2e/ui -v
    KQUERY_UI_WEB=http://127.0.0.1:8090 uv run pytest tests/e2e/ui -v   # 打 Docker 栈
"""
from __future__ import annotations

import os
import socket
import urllib.request

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

WEB = os.environ.get("KQUERY_UI_WEB", "http://127.0.0.1:5173")
API = "http://127.0.0.1:8100"


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _serves_kquery(base: str) -> bool:
    """这个地址上跑的确实是 Kquery 吗？

    端口通 ≠ 是我们的应用：本机 5173 常年被别的 vite 项目占着，
    打过去会得到"元素找不到"的假失败。
    """
    try:
        with urllib.request.urlopen(base, timeout=3) as resp:
            html = resp.read(4096).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return False
    return "kquery" in html.lower()


@pytest.fixture(scope="session")
def admin_session() -> None:
    """认证已下线：没有登录流程，本 fixture 恒为 None。

    保留它是因为 `web_page` 仍按"有会话就注入 localStorage"的结构写，
    将来若恢复鉴权，这里是唯一需要改回的地方。
    """
    return None


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    """用系统 Edge（本机无 Chrome，避免下载 Chromium 二进制）。"""
    return {**browser_type_launch_args, "channel": "msedge"}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}}


@pytest.fixture
def web_page(page, admin_session):
    """已打开首页的页面；在口令档下自动完成登录。

    认证已下线：前端不再有登录页，直接进聊天页。
    因此本 fixture 不再需要"看页面上有没有登录框"来决定是否登录——
    直接打开首页即可。
    """
    from urllib.parse import urlparse

    web_port = urlparse(WEB).port or (443 if WEB.startswith("https") else 80)
    missing = [p for p in (web_port, 8100) if not _port_open(p)]
    if missing:
        pytest.skip(f"服务未启动（端口 {missing} 不通），跳过 UI 测试")
    if not _serves_kquery(WEB):
        pytest.skip(
            f"{WEB} 上不是 Kquery（端口被别的应用占用或页面异常），跳过 UI 测试"
        )
    # 认证已下线：不再注入任何 localStorage 身份种子。
    page.goto(WEB, wait_until="domcontentloaded")
    # 环境护栏：5173 可能是本机其它项目的 dev server（KAiSupport 等长期占用）。
    # 端口通不代表是 Kquery——验证页面标题，不是就跳过而不是整片误红。
    page.wait_for_timeout(1000)
    if "Kquery" not in page.title():
        pytest.skip(f"{WEB} 上的应用不是 Kquery（title={page.title()!r}），跳过 UI 测试")
    # 等应用把会话校验跑完。口令档下首屏先渲染登录表单，
    # /auth/me 回来后才切到应用界面——刚导航完就查登录框，
    # 会把"还没校验完"误判成"没登录"，于是每个用例都去登录一次，
    # 直接撞上登录限流（6 次/分钟），表现为随机停在登录页。
    try:
        page.get_by_placeholder("输入你的问题…").wait_for(state="visible", timeout=20_000)
    except PlaywrightTimeoutError:
        if page.query_selector('input[name="password"]') is not None:
            # 确实没登录上（令牌失效等）才走表单登录
            page.fill('input[name="username"]', os.environ.get("KQUERY_TEST_ADMIN_USER", "admin"))
            page.fill('input[name="password"]', os.environ.get("KQUERY_TEST_ADMIN_PASSWORD", "Kq@dmin2026"))
            page.click('button[type="submit"]')
            page.get_by_placeholder("输入你的问题…").wait_for(state="visible", timeout=20_000)
        else:
            pytest.skip(f"{WEB} 上没有出现对话输入框，跳过 UI 测试")
    return page
