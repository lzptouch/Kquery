"""生产环境必须关闭交互式 API 文档（`A-04`）。

## 判定口径

**按环境开关**，不是一刀切：

- `APP_ENV=prod`（或 `production`）→ `/docs`、`/redoc`、`/openapi.json` 全部 404；
- 其它模式（默认 `dev`）→ 三个端点照常可用。

## 为什么不能一刀切

`tests/e2e/test_all_endpoints.py` 依赖 OpenAPI 枚举端点，端点表与文档同步的
守卫也依赖它。把 OpenAPI 整体关掉，等于把这些守卫一起打瞎——**关文档变成了
没人校验端点契约**。

## 为什么不会「点进去 404」

前端没有「接口文档」入口（`frontend/src` 无 `/docs` 引用，已核对），
所以关闭后没有次生的前端降级问题。
"""
from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings


def _build_client(monkeypatch, app_env: str) -> TestClient:
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("DATA_DIR", tmp)
        monkeypatch.setenv("MODEL_PROVIDER", "mock")
        monkeypatch.setenv("VECTOR_STORE", "faiss")
        monkeypatch.setenv("APP_ENV", app_env)
        get_settings.cache_clear()
        from app.main import create_app

        return TestClient(create_app())


@pytest.fixture()
def prod_client(monkeypatch):
    with _build_client(monkeypatch, "prod") as client:
        yield client
    get_settings.cache_clear()


@pytest.fixture()
def dev_client(monkeypatch):
    with _build_client(monkeypatch, "dev") as client:
        yield client
    get_settings.cache_clear()


def test_prod_disables_docs(prod_client):
    """生产：/docs、/redoc、/openapi.json 全部 404。"""
    assert prod_client.get("/docs").status_code == 404
    assert prod_client.get("/redoc").status_code == 404
    assert prod_client.get("/openapi.json").status_code == 404


def test_prod_keeps_business_endpoints(prod_client):
    """关文档不影响业务端点（否则就是拿 404 换 404）。"""
    assert prod_client.get("/livez").status_code == 200
    assert prod_client.get("/health").status_code == 200


def test_dev_keeps_docs(dev_client):
    """开发/测试：文档照常可用（端点枚举守卫依赖它）。"""
    assert dev_client.get("/docs").status_code == 200
    resp = dev_client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["openapi"].startswith("3.")


def test_app_env_defaults_to_dev(monkeypatch):
    """默认档位是 dev——「生产忘了配」不该等于「生产开着文档」。"""
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = Settings(_env_file=None)
    assert settings.app_env == "dev"
