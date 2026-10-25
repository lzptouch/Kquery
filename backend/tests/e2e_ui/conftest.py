"""浏览器 UI 端到端测试的共享夹具（真实前端 5173 + 真实后端 8100）。

与 tests/e2e（API 级 TestClient）的分工：
- tests/e2e：接口契约（内存 app，Mock 模型）
- tests/e2e_ui：用户在页面上能不能用——真实点击/输入/可见文本断言

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
    cd backend && uv run pytest tests/e2e_ui -v
    KQUERY_UI_WEB=http://127.0.0.1:8090 uv run pytest tests/e2e_ui -v   # 打 Docker 栈
"""
from __future__ import annotations

import os
import socket
import urllib.request

import pytest

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
def browser_type_launch_args(browser_type_launch_args):
    """用系统 Edge（本机无 Chrome，避免下载 Chromium 二进制）。"""
    return {**browser_type_launch_args, "channel": "msedge"}


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}}


@pytest.fixture
def web_page(page):
    """已打开首页的页面；在口令档下自动完成登录。

    后端有两种部署形态，这里都必须能跑：
    - 身份头档（AUTH_ENABLED=false）：前端 dev 旁路直接以 admin 进入，无需登录；
    - 口令档（AUTH_ENABLED=true）：首屏是登录页，必须先登录。

    因此不写死任何一种，而是**看页面上有没有登录框**再决定。这比让 CI 或
    本机去同步 .env 更可靠——两边只要有一个忘了改，UI 测试就会整片红。
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
    page.goto(WEB, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    if page.query_selector('input[name="password"]') is not None:
        page.fill('input[name="username"]', os.environ.get("KQUERY_TEST_ADMIN_USER", "admin"))
        page.fill('input[name="password"]', os.environ.get("KQUERY_TEST_ADMIN_PASSWORD", "Kq@dmin2026"))
        page.click('button[type="submit"]')
        page.wait_for_timeout(2500)
    return page
