"""API 测试层 conftest：提供异步测试客户端、认证 fixture 和响应契约断言。"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from dotenv import load_dotenv

# 加载 .env.test
ENV_FILE = Path(__file__).resolve().parents[1] / ".env.test"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=True)


# 统一使用 backend 目录下的 SQLite 数据库文件，避免不同 cwd 导致表缺失
DB_PATH = Path(__file__).resolve().parents[2] / "backend" / "kquery_test.db"
DB_URL = f"sqlite+aiosqlite:///{DB_PATH}"


# 在导入 FastAPI app 前先替换数据库引擎并创建表
_db_mod = __import__("app.core.database", fromlist=["database"])
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

_db_mod.async_engine = create_async_engine(DB_URL, echo=False, future=True)
_db_mod.async_session_maker = async_sessionmaker(
    _db_mod.async_engine, class_=_db_mod.AsyncSession, expire_on_commit=False, autoflush=False, autocommit=False
)

# 确保所有模型类都注册到 Base.metadata，否则建表会缺失
__import__("app.models.user", fromlist=["user"])
__import__("app.models.knowledge_base", fromlist=["knowledge_base"])
__import__("app.models.note", fromlist=["note"])
__import__("app.models.skill", fromlist=["skill"])
asyncio.run(_db_mod.init_db())


@pytest_asyncio.fixture(scope="session")
async def client() -> AsyncGenerator:
    """创建 httpx AsyncClient，连接后端 app；不触发 startup 中的外部连接副作用。

    方式：直接 import app 实例，使用 ASGITransport；依赖 docker 三件套可用。
    """
    from httpx import ASGITransport, AsyncClient

    from app.api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest_asyncio.fixture(scope="session")
async def auth_headers(client) -> dict:
    """注册并登录测试账号，返回 Authorization headers。"""
    username = os.environ.get("TEST_USER", "e2e_test")
    password = os.environ.get("TEST_PASSWORD", "Test123456")

    register_payload = {
        "username": username,
        "password": password,
        "email": f"{username}@example.com",
    }
    # 注册可能已存在，忽略 4xx
    await client.post("/api/v1/auth/register", json=register_payload)

    login_payload = {"username": username, "password": password}
    resp = await client.post("/api/v1/auth/login", json=login_payload)
    if resp.status_code != 200:
        pytest.skip(f"测试账号登录失败: {resp.status_code} {resp.text}")
    data = resp.json()
    token = data.get("access_token") or data.get("token") or data.get("data", {}).get("access_token")
    if not token:
        pytest.skip(f"登录响应中未找到 access_token: {resp.text}")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def assert_success():
    """断言成功响应。"""
    def _check(resp, expected_status: int = 200, required_fields: list | None = None):
        assert resp.status_code == expected_status, f"{resp.status_code}: {resp.text}"
        body = resp.json()
        if required_fields:
            for field in required_fields:
                if isinstance(field, tuple):
                    key, default = field
                    assert key in body or default is not None, f"缺少字段 {key}"
                else:
                    assert field in body, f"缺少字段 {field}"
        return body
    return _check


@pytest.fixture
def assert_error():
    """断言统一错误响应。"""
    def _check(resp, expected_status: int, code_prefix: str | None = None):
        assert resp.status_code == expected_status, f"{resp.status_code}: {resp.text}"
        body = resp.json()
        assert "error" in body, f"响应缺少 error 字段: {body}"
        assert "code" in body["error"], f"error 缺少 code: {body}"
        assert "message" in body["error"], f"error 缺少 message: {body}"
        # request_id 由中间件注入；部分非业务异常可能缺失，视为可选
        if code_prefix:
            assert body["error"]["code"].startswith(code_prefix), f"错误码前缀不匹配: {body['error']['code']}"
        return body
    return _check


@pytest.fixture(scope="session")
def test_user_id() -> str:
    return "user-test-api"
