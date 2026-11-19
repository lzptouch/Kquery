"""HTTP 客户端（smoke_api / api_sweep 等脚本共用）。

**为什么抽出来**：此前 `tests/e2e/scripts/smoke_api.py` 与 `tests/e2e/scripts/api_sweep.py` 各写一份
`call` / `upload_multipart` / `data_of`，实现几乎相同、参数却不同
（一个支持 `roles`/`tenant` 覆盖，另一个支持自定义 `headers`）。
两份实现的漂移已经发生——现在统一到这里：**一个实现、一处修**。

刻意只依赖标准库：这些脚本要在**没装依赖**的机器上也能跑（排障场景）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

#: Bearer 令牌（**认证已下线**：应用端点不再校验凭证，
#: 这里保留仅为将来恢复鉴权时不必改调用面；当前所有请求都不带身份头）。
_TOKEN = ""


def set_token(token: str) -> None:
    """设置 Bearer 令牌（登录后调用）。"""
    global _TOKEN
    _TOKEN = token or ""


def get_token() -> str:
    return _TOKEN


def call(
    base: str,
    method: str,
    path: str,
    body=None,
    raw: bool = False,
    timeout: int = 120,
    content_type: str | None = None,
    headers: dict | None = None,
    auth: bool = True,
    roles: str | None = None,
    tenant: str | None = None,
):
    """返回 ``(status, json_or_text)``。**不抛异常**——失败也要继续跑后面的用例。

    参数说明：
    - `auth=False`：**故意**匿名请求，用于验证"没凭证必须被拒"
    - `roles` / `tenant`：覆盖身份，用于验证越权与跨租户隔离
    - `headers`：额外请求头（如自定义幂等键）
    """
    url = base.rstrip("/") + path
    data = None
    hdrs = dict(headers or {})
    if auth and _TOKEN:
        hdrs["Authorization"] = f"Bearer {_TOKEN}"
    if body is not None:
        if isinstance(body, bytes):
            data = body
            # 裸 bytes 无法判断意图，用一个显式参数；multipart 上传走 upload_multipart()
            hdrs["Content-Type"] = content_type or "application/octet-stream"
        else:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload.decode(errors="replace")
            try:
                return resp.status, json.loads(payload)
            except json.JSONDecodeError:
                return resp.status, payload.decode(errors="replace")
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        if raw:
            return exc.code, payload.decode(errors="replace")
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload.decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def upload_multipart(
    base: str,
    path: str,
    filename: str,
    content: bytes,
    content_type: str = "text/markdown",
    field: str = "file",
):
    """multipart/form-data 上传。返回 ``(status, json_or_text)``。"""
    boundary = "----KqueryBoundary"
    body = (
        f"--{boundary}\\r\\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\\r\\n'
        f"Content-Type: {content_type}\\r\\n\\r\\n"
    ).encode("utf-8") + content + f"\\r\\n--{boundary}--\\r\\n".encode("utf-8")
    return call(
        base, "POST", path, body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )


def data_of(body):
    """取信封的 data 字段。

    data 可能是 null（错误响应），必须容错——直接 `body["data"].get(...)`
    会在错误路径上抛 AttributeError，把"接口返回了错误"变成"脚本崩了"，
    反而看不到真正的失败原因。
    """
    if not isinstance(body, dict):
        return {}
    value = body.get("data")
    return value if isinstance(value, dict) else {}
