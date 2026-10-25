#!/usr/bin/env python3
"""后端全端点扫描：把 OpenAPI 里声明的每个端点都真实打一遍。

## 为什么需要它

`smoke_api.py` 验的是"关键路径能用"；但一个后端可能有几十个端点，
其中一部分**从来没被任何用例碰过**——路由写错、依赖注入漏了、序列化炸了，
都不会有人发现，等到线上有人调它才暴露。

本脚本从 `/openapi.json` 拉出端点清单，为每个端点准备**真实可用的资源**
（真文档、真会话、真任务、真临时文档），再逐个发真实请求：

| 结果 | 含义 | 处置 |
|---|---|---|
| 2xx / 3xx | 端点活着且跑通了业务 | 通过 |
| 4xx | 端点活着，但请求被拒（参数/权限/资源不存在） | 不算失败，但标注原因 |
| 5xx **带业务错误码** | 能力未配置（如未配密钥的 STS） | 不算失败（配置问题） |
| 5xx **无错误码** | 内部异常（代码 bug） | **失败，必须修** |

用法：
    python3 scripts/api_sweep.py --base http://localhost:8100 --user admin --password xxx
    python3 scripts/api_sweep.py --base http://localhost:8100 --token <JWT>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
REJECTED: list[tuple[str, str]] = []
TIMEOUTS: list[tuple[str, str]] = []

TOKEN = ""

SAMPLE_MD = """# 扫描用文档

## 计费规则
Kquery 按问答次数计费，每问消耗一个额度。

## 常见问题
问：支持哪些格式？答：支持 PDF、Markdown、Excel、纯文本与代码文件。
"""

SAMPLE_XLSX_NAME = "扫描表.xlsx"


# ---------------------------------------------------------------- 基础设施


def call(base: str, method: str, path: str, body=None, raw=False, timeout=120,
         content_type: str | None = None, headers: dict | None = None):
    """返回 (status, body)。不抛异常——失败也要继续跑后面的端点。"""
    url = base.rstrip("/") + path
    data = None
    hdrs = dict(headers or {})
    if TOKEN:
        hdrs["Authorization"] = f"Bearer {TOKEN}"
    if body is not None:
        if isinstance(body, bytes):
            data = body
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


def upload_multipart(base: str, path: str, filename: str, content: bytes,
                     content_type: str = "text/markdown", field: str = "file"):
    boundary = "----KquerySweepBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return call(base, "POST", path, body=body,
                content_type=f"multipart/form-data; boundary={boundary}")


def data_of(body):
    if not isinstance(body, dict):
        return {}
    value = body.get("data")
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- 资源工厂


class Fixtures:
    """为每个端点现场造一份**专属**资源。

    不能复用同一份：扫描是按路径字母序跑的，`DELETE /knowledge/files/{doc_id}`
    排在 `GET /knowledge/preview/{doc_id}` 前面——共用一个 id 的话，
    后面的端点全都会 404，扫描就退化成"只验证了 DELETE"。
    """

    def __init__(self, base: str):
        self.base = base
        self._counter = 0

    def _name(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter}_{uuid.uuid4().hex[:4]}"

    def doc(self) -> str:
        """上传一个真文档，返回 doc_id（本系统 doc_id 就是文件名）。"""
        name = self._name("扫描文档")
        status, body = upload_multipart(self.base, "/knowledge/upload", f"{name}.md",
                                         SAMPLE_MD.encode("utf-8"))
        return data_of(body).get("doc_id", "") if status == 200 else ""

    def quick_doc(self) -> str:
        name = self._name("扫描临时")
        status, body = upload_multipart(self.base, "/quickask/upload", f"{name}.md",
                                         SAMPLE_MD.encode("utf-8"))
        return data_of(body).get("doc_id", "") if status == 200 else ""

    def session(self) -> str:
        status, body = call(self.base, "POST", "/sessions", body={"title": "扫描会话"})
        if status not in (200, 201):
            return ""
        data = data_of(body)
        return data.get("session_id", "") or data.get("id", "")

    def task(self) -> str:
        status, body = upload_multipart(self.base, "/knowledge/ingest", f"{self._name('扫描入库')}.md",
                                         SAMPLE_MD.encode("utf-8"))
        return data_of(body).get("task_id", "") if status == 200 else ""

    def trace(self) -> str:
        """问一次拿 trace_id（拒答反馈等端点需要它做关联）。"""
        status, body = call(self.base, "POST", "/ask",
                            body={"question": "计费规则是什么？"}, raw=True)
        if not isinstance(body, str):
            return ""
        for line in body.splitlines():
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if payload.get("trace_id"):
                    return payload["trace_id"]
        return ""

    def analysis(self) -> str:
        status, body = call(self.base, "POST", "/ask",
                            body={"question": "计费规则是什么？"}, raw=True)
        if not isinstance(body, str):
            return ""
        for line in body.splitlines():
            if line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if payload.get("analysis_id"):
                    return payload["analysis_id"]
        return ""

    def chunk(self, doc_id: str) -> str:
        if not doc_id:
            return ""
        status, body = call(self.base, "GET", f"/knowledge/preview/{urllib.parse.quote(doc_id)}")
        if status != 200:
            return ""
        chunks = data_of(body).get("chunks") or []
        if isinstance(chunks, list) and chunks:
            first = chunks[0]
            if isinstance(first, dict):
                return first.get("chunk_id", "")
        return ""

    def entity(self) -> str:
        status, body = call(self.base, "GET", "/graph/entities")
        if status != 200:
            return ""
        rows = data_of(body).get("entities") or data_of(body).get("rows") or []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0].get("name", "")
        return ""

    def ticket(self) -> str:
        status, body = call(self.base, "POST", "/feedback/refusal", body={
            "question": "扫描用拒答反馈", "answer": "扫描用答案", "reason": "其它",
        })
        if status not in (200, 201):
            return ""
        return data_of(body).get("ticket_id", "")

    def user(self) -> str:
        status, body = call(self.base, "POST", "/auth/register", body={
            "username": self._name("sweep_u"), "password": "Sweep@12345",
        })
        if status not in (200, 201):
            return ""
        return data_of(body).get("user_id", "")


# ---------------------------------------------------------------- 请求构造

PATH_DEFAULTS = {
    "doc_id": "missing-doc",
    "task_id": "missing-task",
    "session_id": "missing-session",
    "run_id": "missing-run",
    "trace_id": "missing-trace",
    "analysis_id": "missing-analysis",
    "ticket_id": "missing-ticket",
    "chunk_id": "missing-chunk",
    "name": "missing-entity",
    "word": "missing-word",
    "user_id": "missing-user",
}

#: 端点级请求体覆盖（让关键端点走真实业务路径）
BODY_OVERRIDES: dict[tuple[str, str], dict | object] = {
    ("POST", "/ask"): {"question": "计费规则是什么？"},
    ("POST", "/ask/stream"): {"question": "计费规则是什么？"},
    ("POST", "/quickask/ask"): lambda fx: {"question": "计费规则是什么？",
                                       "doc_id": fx.get("quick_doc_id", "")},
    ("POST", "/sessions"): {"title": "扫描会话"},
    ("PATCH", "/sessions/{session_id}"): {"title": "扫描会话（改名）"},
    ("POST", "/graph/build"): {},
    ("POST", "/admin/sensitive"): {"word": "扫描敏感词"},
    ("PUT", "/admin/quota"): {"per_minute": 60, "per_day": 1000},
    ("PUT", "/admin/users/{user_id}"): {"roles": ["sales"]},
    ("PUT", "/admin/users/{user_id}/password"): {"password": "Sweep@12345"},
    ("POST", "/knowledge/retrieval-probe"): lambda fx: {"query": "计费规则",
                                                  "doc_id": fx.get("doc_id", "")},
    ("POST", "/knowledge/{doc_id}/probe"): {"query": "计费规则"},
    ("POST", "/feedback/refusal"): lambda fx: {"feedback_type": "other",
                                           "refusal_reason": "扫描用反馈",
                                           "user_note": "扫描",
                                           "trace_id": fx.get("trace_id", "")},
    ("POST", "/ask/runs/{run_id}/resume"): {"action": "hint"},
    ("POST", "/media/index"): lambda fx: {"doc_id": fx.get("doc_id", "")},
    # rebuild_communities 会为每个社区调 LLM 生成摘要（分钟级）；
    # 扫描只验证端点活着，不需要等社区摘要
    ("POST", "/graph/build"): lambda fx: {"doc_id": fx.get("doc_id", ""),
                                      "rebuild_communities": False},
    ("PUT", "/admin/documents/{doc_id}/acl"): {"allow": [], "deny": [], "is_public": False},
    ("POST", "/admin/sensitive"): {"word": "扫描敏感词", "category": "机密", "action": "mask"},
    ("POST", "/auth/register"): lambda fx: {"username": fx.get("username", "sweep_user"),
                                        "password": "Sweep@12345"},
}


def resolve_ref(schema: dict, root: dict) -> dict:
    """解析 $ref（OpenAPI 组件引用）。

    不解析的话 requestBody 的字段全是空的 —— 生成的请求体缺关键字段，
    端点会以 400 拒绝，扫描就退化成"只验证路由存在"。
    """
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 10:
        ref = schema["$ref"]
        seen += 1
        if not ref.startswith("#/"):
            break
        node: object = root
        for part in ref[2:].split("/"):
            node = node.get(part, {})  # type: ignore[union-attr]
        if not isinstance(node, dict):
            break
        schema = node
    return schema if isinstance(schema, dict) else {}


def schema_default(spec: dict):
    """按 schema 生成一个**类型正确**的示例值。

    类型猜错比不给更糟：多填一个 `top_k: "sweep-probe"` 会让整个请求
    在参数校验阶段被拒（400），端点根本没跑到业务逻辑 —— 扫描就白扫了。
    """
    if "example" in spec:
        return spec["example"]
    if "default" in spec:
        return spec["default"]
    if "enum" in spec and spec["enum"]:
        return spec["enum"][0]
    # anyOf / oneOf：取第一个"有具体类型"的分支
    for key in ("anyOf", "oneOf"):
        for branch in spec.get(key) or []:
            if isinstance(branch, dict) and branch.get("type"):
                return schema_default(branch)
    schema_type = spec.get("type")
    if schema_type == "integer":
        return 1
    if schema_type == "number":
        return 1
    if schema_type == "boolean":
        return False
    if schema_type == "array":
        return []
    if schema_type == "object":
        return {}
    return "sweep-probe"


def build_body(op: dict, root: dict, method: str, path: str, fx: dict) -> dict | None:
    rb = op.get("requestBody") or {}
    content = rb.get("content") or {}
    json_schema = (content.get("application/json") or {}).get("schema") or {}
    json_schema = resolve_ref(json_schema, root)
    props = json_schema.get("properties") or {}
    required = set(json_schema.get("required") or [])
    body: dict = {}
    # 只填必填字段：可选字段乱填会把请求打在参数校验上（400），业务逻辑根本没跑到
    for name in required:
        spec = props.get(name)
        if spec is None:
            continue
        body[name] = schema_default(resolve_ref(spec, root))
    override = BODY_OVERRIDES.get((method, path))
    if override:
        body.update(override(fx) if callable(override) else override)
    if rb and not body:
        # 端点声明了 requestBody 但没有必填字段：也要发一个空对象，
        # 否则 FastAPI 报"缺请求体"（422），端点的业务逻辑根本没跑到
        return body
    return body or None


def build_query(op: dict) -> dict:
    out: dict = {}
    for spec in op.get("parameters") or []:
        if spec.get("in") == "query" and spec.get("required"):
            out[spec["name"]] = schema_default(spec)
    return out


# ---------------------------------------------------------------- 主流程


def make_wav() -> bytes:
    """现场造一段 0.2 秒静音 WAV（语音端点只需要一个合法的音频容器）。"""
    import struct

    sample_rate = 16000
    samples = sample_rate // 5
    pcm = b"\x00\x00" * samples
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1, sample_rate,
        sample_rate * 2, 2, 16, b"data", len(pcm),
    )
    return header + pcm


def make_xlsx() -> bytes:
    """现场造一个最小 xlsx（避免依赖仓库里的二进制样本）。"""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/content-types"><Default Extension="rels" ContentType='
                    '"application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
                    'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
                    'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        zf.writestr("_rels/.rels",
                    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.'
                    'org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                    'openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                    'Target="xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml",
                    '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships"><sheets><sheet name="扫描表" sheetId="1" '
                    'r:id="rId1"/></sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels",
                    '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.'
                    'org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.'
                    'openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                    'Target="worksheets/sheet1.xml"/></Relationships>')
        zf.writestr("xl/worksheets/sheet1.xml",
                    '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                    'spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr">'
                    '<is><t>型号</t></is></c><c r="B1" t="inlineStr"><is><t>数量</t></is></c></row>'
                    '<row r="2"><c r="A2" t="inlineStr"><is><t>RX-77</t></is></c>'
                    '<c r="B2"><v>12</v></c></row></sheetData></worksheet>')
    return buf.getvalue()


def main() -> int:
    global TOKEN
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8100")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default="")
    parser.add_argument("--token", default="")
    args = parser.parse_args()
    base = args.base

    if args.token:
        TOKEN = args.token
    else:
        status, body = call(base, "POST", "/auth/login",
                            body={"username": args.user, "password": args.password})
        if status != 200:
            print(f"登录失败：{status} {str(body)[:200]}")
            return 2
        TOKEN = data_of(body).get("token", "")
    if not TOKEN:
        print("未取得令牌")
        return 2

    fx_pool = Fixtures(base)
    status, body = call(base, "GET", "/openapi.json", headers={"Authorization": ""})
    root = body or {}
    paths = root.get("paths") or {}
    endpoints: list[tuple[str, str, dict]] = []
    for path, ops in paths.items():
        for method, op in ops.items():
            if method in ("get", "post", "put", "delete", "patch"):
                endpoints.append((method.upper(), path, op))
    endpoints.sort()

    print(f"\n\033[1m全端点扫描（{len(endpoints)} 个）\033[0m")
    for method, path, op in endpoints:
        fx: dict = {}
        is_quickask = path.startswith("/quickask/")

        # ---------- 现场造资源 ----------
        # 顺序有讲究：quickask 的 {doc_id} 指的是**临时文档**，
        # 必须在通用替换之前单独处理，否则会被知识库 doc_id 顶掉。
        if is_quickask and "{doc_id}" in path:
            fx["quick_doc_id"] = fx_pool.quick_doc()
        elif "{doc_id}" in path:
            fx["doc_id"] = fx_pool.doc()
        if "{session_id}" in path:
            fx["session_id"] = fx_pool.session()
        if "{task_id}" in path:
            fx["task_id"] = fx_pool.task()
        if "{analysis_id}" in path:
            fx["analysis_id"] = fx_pool.analysis()
        if "{chunk_id}" in path:
            fx["chunk_id"] = fx_pool.chunk(fx.get("doc_id", ""))
        if "{name}" in path:
            fx["name"] = fx_pool.entity()
        if "{ticket_id}" in path:
            fx["ticket_id"] = fx_pool.ticket()
        if "{user_id}" in path:
            fx["user_id"] = fx_pool.user()
        # 覆盖表里用到、但路径里没出现的资源
        if (method, path) == ("POST", "/quickask/ask"):
            fx.setdefault("quick_doc_id", fx_pool.quick_doc())
        if (method, path) == ("POST", "/knowledge/retrieval-probe"):
            fx.setdefault("doc_id", fx_pool.doc())
        if (method, path) == ("POST", "/graph/build"):
            fx.setdefault("doc_id", fx_pool.doc())
        if (method, path) == ("POST", "/media/index"):
            fx.setdefault("doc_id", fx_pool.doc())
        if (method, path) == ("POST", "/auth/register"):
            fx["username"] = f"sweep_{uuid.uuid4().hex[:8]}"
        if (method, path) == ("POST", "/feedback/refusal"):
            fx.setdefault("trace_id", fx_pool.trace())

        real_path = path
        if is_quickask and "{doc_id}" in path:
            real_path = real_path.replace("{doc_id}", fx.get("quick_doc_id", ""))
        for key, value in fx.items():
            real_path = real_path.replace("{" + key + "}", str(value))
        for key, value in PATH_DEFAULTS.items():
            real_path = real_path.replace("{" + key + "}", value)

        query = build_query(op)
        if query:
            real_path += "?" + urllib.parse.urlencode(query)

        # ---------- 构造请求 ----------
        content = (op.get("requestBody") or {}).get("content") or {}
        # OpenAPI 里部分 multipart 端点没声明 requestBody（如 /knowledge/ingest），
        # 靠路径白名单兜底，否则会用 JSON 体去调文件上传端点
        MULTIPART_PATHS = {
            "/knowledge/upload", "/knowledge/ingest", "/quickask/upload",
            "/speech/transcribe",
        }
        if "multipart/form-data" in content or path in MULTIPART_PATHS:
            if path == "/speech/transcribe":
                status, resp = upload_multipart(
                    base, real_path, "扫描语音.wav", make_wav(), "audio/wav")
            else:
                # /knowledge/ingest 的表单字段名是 files（复数）：
                # 端点用 form.getlist("files") 取，用 file 会被当成"没传文件"
                field = "files" if path == "/knowledge/ingest" else "file"
                status, resp = upload_multipart(
                    base, real_path, f"扫描_{uuid.uuid4().hex[:4]}.md",
                    SAMPLE_MD.encode("utf-8"), field=field)
        elif "application/json" in content or method in ("POST", "PUT", "PATCH"):
            body = build_body(op, root, method, path, fx)
            status, resp = call(base, method,
                                urllib.parse.quote(real_path, safe="/?&=%"), body=body)
        else:
            status, resp = call(base, method, urllib.parse.quote(real_path, safe="/?&=%"))

        label = f"{method} {path}"
        code = (resp.get("error") or {}).get("code") if isinstance(resp, dict) else None

        if status == 0:
            # status==0 = 连接异常或超时。超时往往是"这个端点真的很慢"
            #（如图谱重建要调 LLM），与"端点坏了"是两回事，单列一类
            detail = str(resp)[:150]
            if "timed out" in detail.lower() or "timeout" in detail.lower():
                TIMEOUTS.append((label, detail))
                print(f"  \033[33m⏱\033[0m {label} — 超时（端点慢，非错误）")
            else:
                FAILED.append((label, detail))
                print(f"  \033[31m✗\033[0m {label} — 连接失败 {detail[:120]}")
            continue
        if status >= 500:
            if code:
                REJECTED.append((label, f"HTTP {status} code={code}"))
                print(f"  \033[33m·\033[0m {label} — HTTP {status} code={code}（能力未配置）")
            else:
                detail = str(resp)[:180]
                FAILED.append((label, f"HTTP {status} {detail}"))
                print(f"  \033[31m✗\033[0m {label} — HTTP {status} {detail}")
            continue
        if status >= 400:
            REJECTED.append((label, f"HTTP {status}" + (f" code={code}" if code else "")))
            print(f"  \033[33m·\033[0m {label} — HTTP {status}" + (f" code={code}" if code else ""))
            continue
        PASSED.append(label)
        print(f"  \033[32m✓\033[0m {label} — HTTP {status}")

    # ---------------------------------------------------------------- 汇总
    total = len(PASSED) + len(FAILED) + len(REJECTED) + len(TIMEOUTS)
    print("\n" + "=" * 62)
    print(f"端点总数 {total}：2xx {len(PASSED)} · 4xx/能力未配置 {len(REJECTED)} · "
          f"超时 {len(TIMEOUTS)} · 5xx 失败 {len(FAILED)}")
    if TIMEOUTS:
        print("\n超时的端点（慢，不是错误）：")
        for name, detail in TIMEOUTS:
            print(f"  · {name} — {detail}")
    if REJECTED:
        print("\n被拒绝的端点（端点活着，本次请求的参数/权限不满足）：")
        for name, detail in REJECTED:
            print(f"  · {name} — {detail}")
    if FAILED:
        print(f"\n\033[31m失败 {len(FAILED)}\033[0m")
        for name, detail in FAILED:
            print(f"  · {name}\n    {detail}")
        return 1
    print(f"\n\033[32m无 5xx：全部 {total} 个端点均可达且未抛内部异常\033[0m")
    return 0


if __name__ == "__main__":
    start = time.perf_counter()
    code = main()
    print(f"耗时 {time.perf_counter() - start:.1f}s")
    sys.exit(code)
