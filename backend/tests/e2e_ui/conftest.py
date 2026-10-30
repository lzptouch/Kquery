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

import json
import os
import socket
import time
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


def _login(username: str, password: str) -> dict | None:
    """调 /auth/login 换一张令牌。失败返回 None（调用方决定怎么兜底）。"""
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{API}/auth/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
    except Exception:  # noqa: BLE001 - 登录失败不是致命错误，交给调用方兜底
        return None
    data = payload.get("data") or {}
    if not data.get("token"):
        return None
    return {
        "user_id": data.get("user_id", username),
        "tenant_id": data.get("tenant_id", "default"),
        "roles": data.get("roles") or ["admin"],
        "token": data["token"],
    }


@pytest.fixture(scope="session")
def admin_session() -> dict | None:
    """整套 UI 测试**只登录一次**，产出的令牌供所有用例复用。

    为什么必须复用：`/auth/login` 对同一登录名限流 6 次/分钟、桶容量 8
    （防爆破，限流本身是对的）。而 UI 用例原本**每条都登录一次**——
    十几条下来必然被限流，被限的那条表现为"莫名其妙停在登录页"，
    页面元素自然一个都找不到，症状与"前端坏了"一模一样。

    所以要改的是**测试的登录次数**，不是放松生产侧的限流。
    """
    user = os.environ.get("KQUERY_TEST_ADMIN_USER", "admin")
    password = os.environ.get("KQUERY_TEST_ADMIN_PASSWORD", "Kq@dmin2026")
    for attempt in range(6):
        session = _login(user, password)
        if session:
            return session
        # 被限流就等一会儿再试（最多约 1 分钟）
        time.sleep(10)
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

    后端有两种部署形态，这里都必须能跑：
    - 身份头档（AUTH_ENABLED=false）：前端 dev 旁路直接以 admin 进入，无需登录；
    - 口令档（AUTH_ENABLED=true）：首屏是登录页，必须先登录。

    因此不写死任何一种，而是**看页面上有没有登录框**再决定。这比让 CI 或
    本机去同步 .env 更可靠——两边只要有一个忘了改，UI 测试就会整片红。

    口令档下登录**一次**（会话级 fixture 换到的令牌），再用 init script
    注入 localStorage——每条用例都去登录会被登录限流挡下（见 admin_session）。
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
    if admin_session:
        # 键名与 frontend/src/lib/auth.ts 的 K 常量保持一致：
        # 少写一个也不影响（前端启动时会用 /auth/me 回填身份），
        # 但写全可以让首屏不必等一次往返。
        #
        # 两个 Playwright 的坑：
        # 1. add_init_script 是**执行脚本体**，不是"调用你传的函数"。
        #    写成 `() => { ... }` 只会创建一个函数表达式——它永远不会被调用，
        #    于是种子静默不生效（表现为"注入了令牌却仍停在登录页"）。
        #    必须写成 IIFE：(() => { ... })()。
        # 2. 1.64 起不再接受 arg 参数，会话值只能内联进脚本体
        #    （json.dumps 产出的是合法 JS 字面量）。
        seed = json.dumps({
            "token": admin_session["token"],
            "user_id": admin_session["user_id"],
            "tenant_id": admin_session["tenant_id"],
            "roles": list(admin_session["roles"]),
        })
        page.add_init_script(
            f"""
            (() => {{
              try {{
                const s = {seed};
                localStorage.setItem("kquery.token", s.token);
                localStorage.setItem("kquery.user_id", s.user_id);
                localStorage.setItem("kquery.tenant_id", s.tenant_id);
                localStorage.setItem("kquery.roles", s.roles.join(","));
              }} catch (e) {{ /* 隐私模式下写不了，无所谓 */ }}
            }})();
            """
        )
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
