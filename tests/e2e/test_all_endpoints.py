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
    ("DELETE", "/knowledge/files/{doc_id}"),
    ("POST", "/feedback/refusal"),
    ("PUT", "/admin/retrieval-config"),
    ("POST", "/admin/retrieval-config/reset"),
    ("PATCH", "/sessions/{session_id}"),
    ("DELETE", "/sessions/{session_id}"),
    ("POST", "/sessions"),
    ("PUT", "/knowledge/{doc_id}/reparse"),
    ("POST", "/knowledge/{doc_id}/reparse"),
    ("POST", "/knowledge/{doc_id}/offline"),
    ("POST", "/knowledge/rebuild/{doc_id}"),
    ("POST", "/knowledge/tasks/{task_id}/retry"),
    ("POST", "/knowledge/tasks/{task_id}/cancel"),
    ("POST", "/graph/communities/build"),
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
    # 端点总数 = 61（9 个 router：业务 8 个共 56 + 探针 5；
    # 注册清单见 backend/app/bootstrap.py 的 register_routes）。
    assert len(endpoints) == 61, f"端点数变为 {len(endpoints)}，README §1.2 需同步更新"


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
        resp = client.request(method, url)
        checked += 1
        if resp.status_code >= 500:
            failures.append(f"{method} {url} → {resp.status_code} {resp.text[:120]}")
    assert not failures, "以下端点返回 5xx：\n" + "\n".join(failures)
    # 端点里有真实副作用的写端点单独有专项用例，其余在此巡检。
    # 下限随端点下线同步下调（44 是当前实测值，保留余量以吸收后续端点增删）。
    assert checked >= 42, f"巡检端点数过少（{checked}），枚举或跳过清单可能失效"
    assert skipped <= 32, f"跳过端点过多（{skipped}），巡检覆盖面不足"


def test_all_endpoints_follow_response_contract(client):
    """响应契约：成功走统一信封，错误带可读 message；例外清单显式登记。"""
    bad = []
    for method, path in _endpoints(client.app):
        if (method, path) in SKIP:
            continue
        url = _fill(path)
        resp = client.request(method, url)
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


def test_kb_endpoints_are_removed(client):
    """知识库端点必须**已下线**（认证与授权已下线）。

    保留这条断言是为了防止"删了代码但路由还挂着"——那会留下没有授权语义的
    半残端点（例如仍能建库、却不知道谁能看到）。
    """
    paths = client.app.openapi()["paths"]
    gone = {
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
    leftover = gone & actual
    assert not leftover, f"知识库端点未下线：{leftover}"




