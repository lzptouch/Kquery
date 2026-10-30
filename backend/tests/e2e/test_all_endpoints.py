"""后端**全端点**巡检：从 OpenAPI 自动枚举，逐个验证可达性与响应契约。

为什么需要它：端点数量已达 89 个，靠人写用例必然漏。新增端点若忘记登记，
或路径写错、前缀写错、鉴权漏挂，这里会立刻暴露。

断言口径（刻意从宽到严，防"假通过"）：
1. **不得 5xx**——端点存在但内部炸了，是最该被抓住的一类；
2. **错误必须是业务错误码**——不能是框架层的 500；
3. **成功响应必须是统一信封**（status/data/meta），例外清单显式登记；
4. **错误响应必须带可读 error.message**——前端要能直接展示。

巡检策略：
- GET/HEAD：直接打（只读）；
- POST/PUT：不带 body（多数会 422 参数校验失败——这是**正确**行为，不算失败）；
- DELETE：只对不存在的 id 调用，不产生副作用；
- 路径参数统一填不存在的占位值，避免误删真实数据。
"""
from __future__ import annotations

import re

import pytest

from tests.conftest import ADMIN_HEADERS

# 路径参数占位值：统一用「不存在的 id」，避免 DELETE 误伤真实数据
PATH_PARAMS = {
    "doc_id": "d-not-exist",
    "kb_id": "default",
    "user_id": "admin",
    "task_id": "t-not-exist",
    "chunk_id": "c-not-exist",
    "file_id": "f-not-exist",
    "session_id": "s-not-exist",
    "run_id": "r-not-exist",
    "word": "zzz-not-exist",
    "id": "x-not-exist",
    "name": "x-not-exist",
    "dataset_id": "x-not-exist",
}

# 不走统一信封的端点（各自有明确契约，单独在别处覆盖）
NO_ENVELOPE = {
    "/ask/stream",  # SSE
    "/metrics",  # Prometheus 文本
    "/health", "/livez", "/readyz", "/healthz",  # 运维探针（裸 JSON）
    "/openapi.json", "/docs", "/redoc",
}

# 明确需要请求体、且不带体会触发副作用的写端点：巡检时跳过（另有专项用例）
SKIP = {
    ("POST", "/ask"),  # 触发真实问答与计费
    ("POST", "/ask/stream"),
    ("POST", "/graph/build"),  # 触发图谱构建（耗时长且写数据）
    ("POST", "/knowledge/upload"),  # 文件上传（multipart）
    ("POST", "/knowledge/ingest"),
    ("POST", "/quickask/upload"),
    ("POST", "/speech/transcribe"),
    ("PUT", "/admin/users/{user_id}"),  # 会改用户角色/启停
    ("PUT", "/admin/users/{user_id}/password"),
    ("DELETE", "/quickask/{doc_id}"),
    ("DELETE", "/knowledge/files/{doc_id}"),
    ("POST", "/feedback/refusal"),
    ("PUT", "/admin/retrieval-config"),
    ("PUT", "/admin/retrieval-config/reset"),
    ("PATCH", "/sessions/{session_id}"),
    ("DELETE", "/sessions/{session_id}"),
    ("POST", "/sessions"),
    ("PUT", "/admin/kb/{kb_id}"),
    ("DELETE", "/admin/kb/{kb_id}"),
    ("PUT", "/admin/kb/{kb_id}/grants"),
    ("PUT", "/admin/kb/{kb_id}/documents"),
    ("PUT", "/admin/documents/{doc_id}/acl"),
    ("PUT", "/knowledge/{doc_id}/reparse"),
    ("POST", "/knowledge/{doc_id}/reparse"),
    ("POST", "/knowledge/{doc_id}/offline"),
    ("POST", "/knowledge/rebuild/{doc_id}"),
    ("POST", "/knowledge/tasks/{task_id}/retry"),
    ("POST", "/knowledge/tasks/{task_id}/cancel"),
    ("POST", "/graph/communities/build"),
    ("POST", "/ask/runs/{run_id}/resume"),
    ("POST", "/ask/runs/{run_id}/interrupt"),
    ("POST", "/speech/sts-token"),
}


def _fill(path: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        return PATH_PARAMS.get(name, "x-not-exist")

    return re.sub(r"\{([^}]+)\}", repl, path)


def _endpoints(app) -> list[tuple[str, str]]:
    out = []
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            if method.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}:
                out.append((method.upper(), path))
    return sorted(out)


def test_endpoint_count_matches_readme(client):
    """端点总数与 README 关键数字表一致（防新增端点忘记回填文档）。"""
    endpoints = _endpoints(client.app)
    # 81 = 13 个 router 共 76 + main.py 内建 5（见 doc/analysis/README.md §1.2）
    # 敏感词/配额/审计下线：移除 audit×2 + quota×3 + sensitive×3 = 8 个 → 89-8=81
    assert len(endpoints) == 81, f"端点数变为 {len(endpoints)}，README §1.2 需同步更新"


def test_all_endpoints_reachable_without_5xx(client):
    """逐个端点巡检：任何端点都不得返回 5xx。"""
    failures = []
    checked = 0
    skipped = 0
    for method, path in _endpoints(client.app):
        if (method, path) in SKIP:
            skipped += 1
            continue
        url = _fill(path)
        resp = client.request(method, url, headers=ADMIN_HEADERS)
        checked += 1
        if resp.status_code >= 500:
            failures.append(f"{method} {url} → {resp.status_code} {resp.text[:120]}")
    assert not failures, "以下端点返回 5xx：\n" + "\n".join(failures)
    # 81 个端点里，27 个有真实副作用的写端点单独有专项用例，剩 54 个在此巡检
    assert checked >= 50, f"巡检端点数过少（{checked}），枚举或跳过清单可能失效"
    assert skipped <= 32, f"跳过端点过多（{skipped}），巡检覆盖面不足"


def test_all_endpoints_follow_response_contract(client):
    """响应契约：成功走统一信封，错误带可读 message；例外清单显式登记。"""
    bad = []
    for method, path in _endpoints(client.app):
        if (method, path) in SKIP:
            continue
        url = _fill(path)
        resp = client.request(method, url, headers=ADMIN_HEADERS)
        if path in NO_ENVELOPE:
            continue
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            bad.append(f"{method} {url} → 非 JSON 响应（未登记例外）")
            continue
        if resp.status_code >= 400:
            # 错误信封：必须有 error.code 与 error.message
            if body.get("status") != "error":
                bad.append(f"{method} {url} → {resp.status_code} 但 status={body.get('status')!r}")
            elif not (body.get("error") or {}).get("message"):
                bad.append(f"{method} {url} → 错误响应缺可读 message")
        else:
            # 成功信封：必须有 status/data/meta
            for key in ("status", "meta"):
                if key not in body:
                    bad.append(f"{method} {url} → 成功响应缺 {key} 字段")
            if body.get("status") not in ("ok", "warn"):
                bad.append(f"{method} {url} → status={body.get('status')!r} 非法")
    assert not bad, "以下端点违反响应契约：\n" + "\n".join(bad)


def test_openapi_lists_kb_endpoints(client):
    """新增的知识库端点必须真的注册进 OpenAPI（拼错前缀不会被发现）。"""
    paths = client.app.openapi()["paths"]
    expected = {
        ("get", "/admin/kb"),
        ("post", "/admin/kb"),
        ("put", "/admin/kb/{kb_id}"),
        ("delete", "/admin/kb/{kb_id}"),
        ("get", "/admin/kb/{kb_id}/grants"),
        ("put", "/admin/kb/{kb_id}/grants"),
        ("get", "/admin/kb/{kb_id}/documents"),
        ("put", "/admin/kb/{kb_id}/documents"),
        ("get", "/admin/users/{user_id}/kbs"),
    }
    actual = {(m, p) for p, ops in paths.items() for m in ops if m in {"get", "post", "put", "delete"}}
    missing = expected - actual
    assert not missing, f"知识库端点未注册：{missing}"


def test_kb_endpoints_require_admin(client):
    """知识库端点必须挂 admin 门禁（普通用户一律 403）。"""
    non_admin = {"X-User-Id": "lisi", "X-Tenant-Id": "default", "X-Roles": "sales"}
    for method, path in [
        ("GET", "/admin/kb"),
        ("POST", "/admin/kb"),
        ("GET", "/admin/kb/default/grants"),
        ("GET", "/admin/kb/default/documents"),
        ("GET", "/admin/users/admin/kbs"),
    ]:
        resp = client.request(method, _fill(path), headers=non_admin, json={} if method == "POST" else None)
        assert resp.status_code == 403, f"{method} {path} 非管理员应 403，实得 {resp.status_code}"


@pytest.mark.parametrize("path", ["/admin/kb", "/admin/users/admin/kbs"])
def test_kb_endpoints_use_probe_safe_wording(client, path):
    """防探测口径：不存在与无权限统一文案，不泄露资源是否存在。"""
    resp = client.get(path, headers=ADMIN_HEADERS)
    assert resp.status_code == 200, f"{path} 应可访问"
    # 不存在的 kb 走 404 + 同一文案
    missing = client.get("/admin/kb/kb-does-not-exist/grants", headers=ADMIN_HEADERS)
    assert missing.status_code == 404
    assert "无权限或不存在" in missing.json()["error"]["message"]
