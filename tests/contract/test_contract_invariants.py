"""端点契约不变量（`X-30`）：从 `app.openapi()` 枚举，不写死端点清单。

## 与 `tests/e2e/test_all_endpoints.py` 的分工

- e2e 巡检：**可达性 + 响应形态**（不得 5xx、成功/失败都是信封、错误码是业务码）；
- 本文件：**跨端点的结构性约束**，这些约束靠单个端点用例永远测不到：
  1. 文档里声明的每个 (method, path) 都真实挂载（404 不能是"路由未命中"）；
  2. 所有 JSON 错误响应的 `error.code` 都落在 `ErrCode` 值域内；
  3. 404 必须分两层：路由未命中（1004）≠ 资源不存在（3xxx）；
  4. 405 / 未知路径同样是统一信封，不是框架默认的 `{"detail": ...}`；
  5. `X-Trace-Id` 在请求头、响应头、`meta.trace_id` 三处一致（可观测的基础）；
  6. 业务端点必须在 OpenAPI 里声明 `Envelope` 作为 200 响应模型；
  7. 所有带路径参数的操作都必须显式归类为"归属校验"端点——
     新增一个 `/{id}` 端点却不做归属断言，会在这里变红。

## 为什么枚举而不是清单

硬编码清单只能证明"我列的那些还在"；新端点可以悄悄绕过统一信封/错误码。
枚举把"漏登记例外"变成红灯：`NON_ENVELOPE_200` 里每一条都要写清为什么。
"""
from __future__ import annotations

import re

import pytest

from app.core.envelope import ErrCode
from tests.conftest import _client_with_env

#: 成功响应不是统一信封的端点（200 响应模型不是 `Envelope`）。
#: 逐条登记理由：新增这类端点必须同步这里，否则契约测试会失败。
NON_ENVELOPE_200 = {
    ("GET", "/health"),  # 运维探针：裸 {"status": "up"}，网关按字段取值
    ("GET", "/healthz"),  # 同上（liveness）
    ("GET", "/livez"),  # 同上
    ("GET", "/readyz"),  # 同上（readiness）
    ("GET", "/metrics"),  # Prometheus 文本格式
    ("GET", "/assets/{name}"),  # 图片等二进制资产
    ("GET", "/ask/stream/{trace_id}"),  # SSE 续传：成功即事件流
    ("POST", "/ask/stream"),  # SSE
}

#: 带路径参数的操作 → 必须做归属（租户/用户/run 前缀）校验。
#: 全部逐个核对过；新增 `/{id}` 端点必须先在对应模块实现归属断言再登记到这里。
PATH_PARAM_OPERATIONS = {
    ("POST", "/ask/runs/{run_id}/cancel"),  # _assert_run_owner
    ("GET", "/ask/stream/{trace_id}"),  # 泵记录的 tenant/user 比对
    ("GET", "/ask/{trace_id}/cost"),  # CostLedger.for_trace(tenant_id)
    ("GET", "/assets/{name}"),  # ImageStore 按 tenant/asset_id 取
    ("GET", "/feedback/{ticket_id}"),  # FeedbackStore.get(tenant, user)
    ("GET", "/graph/build/{task_id}"),  # 构建任务按 tenant 查
    ("GET", "/graph/entity/{name}"),  # 图谱按 tenant 读
    ("DELETE", "/knowledge/files/{doc_id}"),  # _assert_doc_tenant
    ("GET", "/knowledge/preview/{doc_id}"),  # _assert_doc_tenant
    ("GET", "/knowledge/preview/{doc_id}/chunk/{chunk_id}"),  # _assert_doc_tenant
    ("GET", "/knowledge/query-analysis/{analysis_id}"),  # AnalysisStore.get(tenant)
    ("POST", "/knowledge/rebuild/{doc_id}"),  # _assert_doc_tenant
    ("GET", "/knowledge/tasks/{task_id}"),  # 任务按 tenant 查
    ("POST", "/knowledge/tasks/{task_id}/cancel"),  # 任务按 tenant 取
    ("POST", "/knowledge/tasks/{task_id}/retry"),  # 任务按 tenant 取
    ("POST", "/knowledge/{doc_id}/offline"),  # _assert_doc_tenant
    ("POST", "/knowledge/{doc_id}/probe"),  # _assert_doc_tenant
    ("POST", "/knowledge/{doc_id}/reparse"),  # _assert_doc_tenant
    ("GET", "/sessions/{session_id}"),  # SessionStore.get(tenant, user)
    ("PATCH", "/sessions/{session_id}"),  # 同上
    ("DELETE", "/sessions/{session_id}"),  # 同上
}

_OPERATION_METHODS = {"get", "post", "put", "delete", "patch"}


def _endpoints(app) -> list[tuple[str, str]]:
    """OpenAPI 里声明的全部 (METHOD, path)，排序保证失败信息稳定。"""
    out: list[tuple[str, str]] = []
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            if method.lower() in _OPERATION_METHODS:
                out.append((method.upper(), path))
    return sorted(out)


def _fill(path: str) -> str:
    """路径参数统一填「不存在的占位值」，避免误伤真实数据。"""
    return re.sub(r"\{[^}]+\}", "x-not-exist", path)


def _probe(client, method: str, path: str):
    """最小请求：写方法带 `json={}`（拿到参数校验错误，而不是 422 空 body）。"""
    kwargs = {"json": {}} if method in {"POST", "PUT", "PATCH"} else {}
    return client.request(method, _fill(path), **kwargs)


def _json_body(resp):
    if "json" not in resp.headers.get("content-type", ""):
        return None
    try:
        return resp.json()
    except ValueError:  # 声明 JSON 但体不是 JSON
        return None


def _error_code(body) -> int | None:
    if not isinstance(body, dict):
        return None
    return (body.get("error") or {}).get("code")


@pytest.fixture(scope="module")
def probe_results():
    """模块级探针：所有端点各打一次，供多条断言复用（避免重复 72 次请求）。"""
    with _client_with_env() as client:
        rows = []
        for method, path in _endpoints(client.app):
            resp = _probe(client, method, path)
            rows.append((method, path, resp, _json_body(resp)))
        yield client, rows


class TestRouteInventory:
    def test_every_documented_operation_is_routed(self, probe_results):
        """声明的端点必须真实挂载：不允许 405，也不允许「路由未命中」的 404。"""
        _client, rows = probe_results
        broken = []
        for method, path, resp, body in rows:
            if resp.status_code == 405:
                broken.append(f"{method} {path} → 405（方法未挂载）")
            elif resp.status_code == 404 and _error_code(body) == ErrCode.INVALID_REQUEST:
                broken.append(f"{method} {path} → 404/1004（路由未命中）")
        assert not broken, "OpenAPI 与真实路由不一致：\n" + "\n".join(broken)

    def test_path_parameter_operations_are_classified(self, probe_results):
        """每个带 `{param}` 的操作都必须在本文件登记为「已做归属校验」。

        这不是形式主义：一个只按 id 取资源、不断言归属的端点，在多租户回归时
        就是越权面。登记表强迫作者在新增时给出归属依据（注释里写在哪个函数）。
        """
        client, _rows = probe_results
        actual = {e for e in _endpoints(client.app) if "{" in e[1]}
        assert actual == PATH_PARAM_OPERATIONS, (
            "带路径参数的操作集合变化：\n"
            f"  新增未归类: {sorted(actual - PATH_PARAM_OPERATIONS)}\n"
            f"  已下线未清理: {sorted(PATH_PARAM_OPERATIONS - actual)}"
        )

    def test_removed_auth_surface_stays_removed(self, client):
        """认证/授权下线后不得复活半残端点。

        复活 `/auth/*` 或 `/admin/users` 而不恢复配套的授权语义，会得到一个
        "能登录但没有任何权限模型"的表面能力——比没有更危险。
        """
        paths = client.app.openapi()["paths"]
        leftovers = [
            p for p in paths
            if p.startswith("/auth/") or p.startswith("/admin/users") or p.startswith("/admin/kb")
        ]
        assert not leftovers, f"已下线的认证/授权端点被重新挂载：{leftovers}"


class TestErrorTaxonomy:
    def test_all_error_codes_are_declared_in_enum(self, probe_results):
        """所有 JSON 错误响应的 code 必须属于 `ErrCode`，且 message 可读。"""
        _client, rows = probe_results
        valid = {int(code) for code in ErrCode}
        bad = []
        for method, path, resp, body in rows:
            if body is None or resp.status_code < 400:
                continue
            code = _error_code(body)
            if code not in valid:
                bad.append(f"{method} {path} → {resp.status_code} code={code!r}")
            elif not (body.get("error") or {}).get("message"):
                bad.append(f"{method} {path} → {resp.status_code} 缺 error.message")
        assert not bad, "以下端点使用了未声明的错误码：\n" + "\n".join(bad)

    def test_route_miss_and_resource_miss_are_distinguishable(self, client):
        """404 分两层：路由不存在 → 1004；路由存在但资源不存在 → 3xxx/4xxx。

        混为一谈会让前端无法区分「接口写错了」和「数据没了」，
        也会让错误看板上的 404 失去意义。
        """
        miss_route = client.get("/no/such/path")
        assert miss_route.status_code == 404
        assert _error_code(miss_route.json()) == ErrCode.INVALID_REQUEST

        miss_resource = client.get("/sessions/s-not-exist")
        assert miss_resource.status_code == 404
        assert _error_code(miss_resource.json()) == ErrCode.SESSION_NOT_FOUND

    def test_method_not_allowed_uses_envelope(self, client):
        """405 也走统一信封，而不是 FastAPI 默认的 `{"detail": "Method Not Allowed"}`。"""
        resp = client.put("/ask", json={})
        assert resp.status_code == 405
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == ErrCode.INVALID_REQUEST
        assert "detail" not in body

    def test_error_responses_do_not_leak_internals(self, probe_results):
        """错误体只应有 code/message/meta：不得夹带堆栈、源码路径或内部类名。"""
        _client, rows = probe_results
        forbidden = ("Traceback", "site-packages", "/backend/app/", "app.services.")
        leaks = []
        for method, path, resp, body in rows:
            if not isinstance(body, dict) or resp.status_code < 400:
                continue
            text = str(body)
            for token in forbidden:
                if token in text:
                    leaks.append(f"{method} {path} → 包含 {token!r}")
        assert not leaks, "错误响应泄漏内部信息：\n" + "\n".join(leaks)


class TestTracingAndSchema:
    def test_trace_id_roundtrips_through_header_and_meta(self, client):
        """请求头 `X-Trace-Id` 必须原样出现在响应头与信封 meta 中。

        续传（`Last-Event-ID`）与日志串联都依赖这条链路；
        任一处断链，线上就只能靠时间戳猜请求。
        """
        trace = "contract-trace-0001"
        resp = client.get("/knowledge/files", headers={"X-Trace-Id": trace})
        assert resp.status_code == 200
        assert resp.headers.get("x-trace-id") == trace
        assert resp.json()["meta"]["trace_id"] == trace

        # 错误路径同样保留（否则排障时最需要的那条反而丢了）
        err = client.get("/sessions/s-not-exist", headers={"X-Trace-Id": trace})
        assert err.status_code == 404
        assert err.headers.get("x-trace-id") == trace
        assert err.json()["meta"]["trace_id"] == trace

    def test_business_endpoints_declare_envelope_schema(self, client):
        """OpenAPI 里 200 响应必须引用 `Envelope`——文档与实现同一份契约。

        没有这一条，`/docs` 会把业务响应展示成一个空 schema，
        前端生成客户端时只能靠猜；也会让"新增端点忘了用信封"绕过所有测试。
        """
        schemas = client.app.openapi()["paths"]
        undeclared = []
        for path, ops in schemas.items():
            for method, op in ops.items():
                if method.lower() not in _OPERATION_METHODS:
                    continue
                key = (method.upper(), path)
                if key in NON_ENVELOPE_200:
                    continue
                schema = (
                    op.get("responses", {})
                    .get("200", {})
                    .get("content", {})
                    .get("application/json", {})
                    .get("schema", {})
                )
                if schema.get("$ref") != "#/components/schemas/Envelope":
                    undeclared.append(f"{method.upper()} {path} → {schema}")
        assert not undeclared, (
            "以下端点未在 OpenAPI 声明 Envelope（若确属例外，请登记到 NON_ENVELOPE_200）：\n"
            + "\n".join(undeclared)
        )

    def test_non_envelope_exceptions_still_exist(self, client):
        """例外表不能无限膨胀：每条都必须仍是"真实存在的端点"。

        否则下线端点后残留的例外条目会继续豁免新端点（例如路径被复用）。
        """
        actual = set(_endpoints(client.app))
        stale = NON_ENVELOPE_200 - actual
        assert not stale, f"例外表里登记了不存在的端点：{sorted(stale)}"
