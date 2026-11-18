"""/healthz 的错误码与状态映射（`A-05`）。

## 需求

`/healthz` 的响应体此前只有异常**类型名**（如 `ConnectError`），没有错误码：
调用方无法按键值判定「哪一类依赖故障」，只能靠字符串匹配——这在改个异常类名时
就会静默失效。

现在：

- 每个 down 的依赖带 `code`（`ErrCode` 52xx 段，分项区分依赖类别）；
- 顶层 `code` 非 0 表示「有依赖不可用」；
- 状态 ↔ HTTP 码映射成文（`PROBE_STATUS_HTTP`：up=200 / down=503）。

## 红线

响应体**不得**出现异常原文、连接串、内部路径——排障靠 `trace_id` 反查日志。
"""
from __future__ import annotations

import pytest

from app.core.envelope import ErrCode
from app.main import PROBE_ERROR_CODES, PROBE_STATUS_HTTP

# 探针清单（与 /healthz 实际探测的依赖一一对应）
PROBE_NAMES = {
    "vector_store",
    "bm25_index",
    "graph_store",
    "shared_state",
    "long_term_memory",
    "parser_router",
    "provider",
    "upstream",
}

SECRET = "redis://user:sup3rsecret@10.0.0.7:6379/0"


class _Boom:
    """探针会碰的属性一碰就抛带连接串的异常（模拟依赖不可达）。"""

    @property
    def total(self):
        raise RuntimeError(SECRET)


def test_every_probe_has_error_code():
    """每个被探测的依赖都必须有对应错误码（漏一个 = 调用方只能猜）。"""
    assert PROBE_NAMES <= set(PROBE_ERROR_CODES)
    for name in PROBE_NAMES:
        code = PROBE_ERROR_CODES[name]
        assert isinstance(code, ErrCode)
        assert int(code) >= 5200, f"{name} 的错误码不在 52xx 段"


def test_error_codes_are_distinct_per_dependency_class():
    """分项码必须真的分项（否则「按码差异化处置」是空话）。"""
    assert PROBE_ERROR_CODES["vector_store"] != PROBE_ERROR_CODES["graph_store"]
    assert PROBE_ERROR_CODES["graph_store"] != PROBE_ERROR_CODES["shared_state"]
    # 文本检索链路（向量库 / BM25）可以同码——它们是同一类故障
    assert PROBE_ERROR_CODES["bm25_index"] == PROBE_ERROR_CODES["vector_store"]


def test_status_http_mapping_is_documented():
    """状态 ↔ HTTP 码映射必须成文且与实现一致。"""
    assert PROBE_STATUS_HTTP == {"up": 200, "down": 503}


def test_healthy_response_has_zero_code(client):
    """全部正常：顶层 code=0，HTTP 200。"""
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert body["code"] == 0
    assert body["down"] == []
    for dep in body["dependencies"]:
        assert dep["status"] == "up"
        assert "code" not in dep  # up 的依赖不需要错误码


def test_down_dependency_carries_error_code(client, monkeypatch):
    """依赖不可用：HTTP 503 + 分项错误码，且**不泄露**连接串/异常原文。"""
    monkeypatch.setattr(client.app.state, "store", _Boom(), raising=False)
    resp = client.get("/healthz")
    assert resp.status_code == PROBE_STATUS_HTTP["down"] == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["code"] == int(ErrCode.DEPENDENCY_UNAVAILABLE)
    assert "vector_store" in body["down"]

    dep = next(d for d in body["dependencies"] if d["name"] == "vector_store")
    assert dep["status"] == "down"
    assert dep["code"] == int(ErrCode.VECTOR_STORE_UNAVAILABLE)
    # 只回异常类型名，不回原文
    assert dep["error"] == "RuntimeError"
    # 红线：连接串 / 路径 / 异常原文不得出现
    assert SECRET not in resp.text
    assert "sup3rsecret" not in resp.text


@pytest.mark.parametrize("name", sorted(PROBE_NAMES))
def test_each_probe_name_maps_to_a_code(name):
    """逐个依赖确认映射表完整（新增探针时这条会提醒补映射）。"""
    assert name in PROBE_ERROR_CODES
