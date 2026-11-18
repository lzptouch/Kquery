"""写端点必须挂限流依赖（`A-20`）。

## 为什么这条要有自动断言

限流漏挂是**静默**的：没有报错、没有告警，只有在流量高峰被打穿时才发现。
认证已下线后，应用层没有任何访问控制，**限流是唯一的流量护栏**——护栏漏一处，
就等于把上游模型配额暴露在公网上。

## 为什么不能用 grep

grep 只能看到**源码写法**，看不到 router 级依赖继承
（`APIRouter(dependencies=[...])` 会把依赖下发给它挂载的所有路由）。
本测试遍历**运行时路由对象**的依赖链，与 FastAPI 实际生效的语义一致。

## 兼容 FastAPI 新版的懒挂载

FastAPI 0.142 起 `include_router` 不再把路由拍平进 `app.routes`，
而是塞进 `_IncludedRouter` 包装器。因此这里**递归展开**：
`_IncludedRouter` → `original_router.routes`。
"""
from __future__ import annotations

from fastapi.routing import APIRoute

from app.dependencies import enforce_rate_limit

# 非幂等 / 有副作用的方法（写端点）
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

#: 语义上不是「查询」的读端点：挂限流是**刻意**的。
#: 判据不是 HTTP 方法，而是「一次请求会不会长期占用服务端资源」。
RESUME_READ_EXCEPTIONS = {
    # SSE 断点续传：每次附着都起一个尾随任务 + 读取缓冲；
    # 客户端疯狂重连等于持续占用生成/缓冲资源，按写流量挡。
    "/ask/stream/{trace_id}",
}


def _iter_routes(router) -> list[APIRoute]:
    """递归展开所有 APIRoute（含 router 级依赖的下发）。"""
    out: list[APIRoute] = []
    for route in getattr(router, "routes", []):
        if isinstance(route, APIRoute):
            out.append(route)
            continue
        original = getattr(route, "original_router", None)
        if original is not None:
            out.extend(_iter_routes(original))
    return out


def _dependency_calls(route: APIRoute) -> set:
    """路由依赖链上的可调用对象集合（按**对象**比对，不靠函数名字符串）。"""
    return {d.call for d in route.dependant.dependencies}


def test_write_endpoints_exist(client):
    """前提校验：确实枚举到了写端点（否则下面的断言会空跑变绿）。"""
    routes = _iter_routes(client.app.router)
    writes = [
        r
        for r in routes
        if WRITE_METHODS & (set(r.methods or set()) - {"HEAD", "OPTIONS"})
    ]
    assert len(writes) >= 20, f"只枚举到 {len(writes)} 个写端点，路由展开逻辑可能坏了"


def test_every_write_endpoint_is_rate_limited(client):
    """所有写端点的依赖链里都必须有 `enforce_rate_limit`。"""
    missing: list[str] = []
    for route in _iter_routes(client.app.router):
        methods = (set(route.methods or set()) - {"HEAD", "OPTIONS"})
        if not (methods & WRITE_METHODS):
            continue
        if enforce_rate_limit not in _dependency_calls(route):
            missing.append(f"{','.join(sorted(methods))} {route.path}")
    assert not missing, "以下写端点没有挂限流依赖：\n  " + "\n  ".join(sorted(missing))


def test_get_endpoints_not_rate_limited(client):
    """反向断言：读端点不该被限流（限流要挡的是写流量，不是查询）。"""
    over_limited: list[str] = []
    for route in _iter_routes(client.app.router):
        methods = (set(route.methods or set()) - {"HEAD", "OPTIONS"})
        if not methods:
            continue
        # 只关心**纯读**端点（既没有写方法）
        if methods & WRITE_METHODS:
            continue
        if route.path in RESUME_READ_EXCEPTIONS:
            continue
        if enforce_rate_limit in _dependency_calls(route):
            over_limited.append(f"{','.join(sorted(methods))} {route.path}")
    assert not over_limited, "以下纯读端点挂了限流：\n  " + "\n  ".join(sorted(over_limited))


def test_rate_limit_actually_rejects(client, monkeypatch):
    """功能断言：令牌桶打满后写请求必须 429（依赖确实生效，不只是挂了个名字）。"""
    monkeypatch.setattr(
        "app.dependencies.resolve_bucket_limits", lambda *args, **kwargs: (0.0, 1.0)
    )
    first = client.post("/sessions", json={"title": "限流验证"})
    assert first.status_code == 200, first.text
    second = client.post("/sessions", json={"title": "限流验证"})
    assert second.status_code == 429, second.text
    assert second.json()["error"]["code"] == 6101  # ErrCode.RATE_LIMITED
