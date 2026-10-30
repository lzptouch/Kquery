"""跨格式一致性（矩阵 §九，XFM-01~XFM-10）—— **所有格式都要过**。

单格式测试回答"这个格式行不行"，跨格式测试回答"**系统对所有格式是不是
一套说法**"。后者才是用户真正感知到的东西：
用户不会关心"PDF 走 3003 而 XLSX 走 3201"，他只关心"传上去之后都有没有
给我一句人话"。格式各说各话的时候，前端就得为每种格式各写一套错误处理——
而实际上没人会这么写，最后就是"显示一个看不懂的错误码"。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.envelope import ApiError, ErrCode
from app.services.parsers.router import SUPPORTED_EXTENSIONS, ParserRouter

from .contract import frontend_allowed_extensions
from .fixtures import ALL_EXTENSION_SAMPLES, sample_bytes


def _router() -> ParserRouter:
    return ParserRouter(
        ocr_parser=None, audio_parser=None, docx_parser=None  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# XFM-01 白名单一致
# --------------------------------------------------------------------------


def test_xfm01_whitelist_identical() -> None:
    """XFM-01：前后端白名单**逐字一致**（P0）。

    守卫测试固化：清单各写各的，迟早对不上。
    """
    backend = set(SUPPORTED_EXTENSIONS)
    frontend = frontend_allowed_extensions()
    assert backend == frontend, (
        "前后端白名单漂移：\n"
        f"  仅后端：{sorted(backend - frontend)}\n"
        f"  仅前端：{sorted(frontend - backend)}"
    )


# --------------------------------------------------------------------------
# XFM-02 不支持扩展名的可读提示
# --------------------------------------------------------------------------


def test_xfm02_unknown_extension_has_readable_hint() -> None:
    """XFM-02：未知扩展名 → message 含「不支持的文件类型」+ 支持列表。"""
    router = _router()
    with pytest.raises(ApiError) as exc:
        router.route(Path("/tmp/x.bin"))
    assert exc.value.code == 3101, f"期望 3101，实际 {exc.value.code}"
    assert "不支持的文件类型" in exc.value.message, f"文案异常：{exc.value.message}"
    assert ".pdf" in exc.value.message, "文案应列出支持的类型"


def test_xfm02_legacy_office_hints() -> None:
    """XFM-02 补充：`.xls` / `.ppt` 的 400 是**成功路径**（引导用户转换）。

    只断言状态码会漏文案回归——所以必须断言文案关键词。
    """
    router = _router()
    for ext, keyword in ((".xls", "xlsx"), (".ppt", "pptx")):
        with pytest.raises(ApiError) as exc:
            router.route(Path(f"/tmp/old{ext}"))
        assert exc.value.code == 3101
        assert keyword in exc.value.message, f"[{ext}] 文案应提示 {keyword}：{exc.value.message}"


# --------------------------------------------------------------------------
# XFM-03 大小写不敏感
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", [".PDF", ".Pdf", ".XLSX", ".MD", ".PNG"])
def test_xfm03_extension_case_insensitive(ext: str) -> None:
    """XFM-03：扩展名大小写不敏感（`.PDF` / `.Pdf` 都可路由）。"""
    router = _router()
    assert router.route(Path(f"/tmp/sample{ext}")) in (
        "pdf", "spreadsheet", "markdown", "ocr"
    )


# --------------------------------------------------------------------------
# XFM-04 错误码分区
# --------------------------------------------------------------------------


def test_xfm04_error_code_partitions() -> None:
    """XFM-04：解析类错误 ∈ {3003, 3201, 3202}；不支持 ∈ {2003, 3101}；超限 = 2004。

    错误码是**给调用方编程用的**，不是给人看的。分区稳定，客户端才能
    按类别做不同的处理（重试 / 引导转换 / 提示换格式）。
    """
    parse_codes = {
        ErrCode.DOCUMENT_PARSE_FAILED.value,
        ErrCode.PARSE_FAILED.value,
        ErrCode.PARSE_ENCRYPTED.value,
    }
    unsupported_codes = {ErrCode.UNSUPPORTED_FILE.value, ErrCode.UNSUPPORTED_FILE_TYPE.value}

    assert parse_codes == {3003, 3201, 3202}, f"解析类错误码分区变了：{parse_codes}"
    assert unsupported_codes == {2003, 3101}, f"不支持类错误码分区变了：{unsupported_codes}"
    assert ErrCode.FILE_TOO_LARGE.value == 2004, "超限错误码应为 2004"


def test_xfm04_corrupt_binary_uses_parse_codes() -> None:
    """XFM-04 补充：损坏的二进制文件必须落在**解析类**错误码里，不是"不支持"。"""
    from app.services.parsers.spreadsheet import SpreadsheetParser

    parser = SpreadsheetParser()
    with pytest.raises(ApiError) as exc:
        parser.parse(_tmp_file(b"not-an-xlsx"), "xfm-4", "default", {})
    assert exc.value.code in (3003, 3201, 3202), (
        f"损坏文件的错误码应落在解析类，实际 {exc.value.code}"
    )


def _tmp_file(data: bytes) -> Path:
    path = Path(tempfile.mkdtemp()) / "x.bin"
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------
# XFM-05 统一信封
# --------------------------------------------------------------------------


def test_xfm05_error_envelope_shape(client: TestClient) -> None:
    """XFM-05：错误响应恒含 `status=="error"` + `error.code` + `error.message`。"""
    resp = client.post(
        "/knowledge/upload",
        files={"file": ("x.bin", b"whatever", "application/octet-stream")},
    )
    assert resp.status_code == 400, f"期望 400，实际 {resp.status_code}"
    body = resp.json()
    assert body["status"] == "error", f"信封形态异常：{body}"
    assert body["error"] is not None and body["error"]["code"], f"缺 error.code：{body}"
    assert body["error"]["message"], f"缺 error.message：{body}"


def test_xfm05_ok_envelope_shape(client: TestClient) -> None:
    """XFM-05 补充：成功响应恒含 `status=="ok"` + `data` + `meta.trace_id`。"""
    resp = client.get("/knowledge/files")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok", f"信封形态异常：{body}"
    assert "data" in body
    assert body["meta"]["trace_id"], f"缺 trace_id：{body}"


# --------------------------------------------------------------------------
# XFM-08 溯源统一
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", sorted(ALL_EXTENSION_SAMPLES))
def test_xfm08_source_is_renderable(ext: str, router: ParserRouter, tmp_path) -> None:
    """XFM-08：所有格式的 segment source 都能被前端渲染（非空、非 undefined）。

    前端溯源栏直接显示 source。它要是 "None" 或 "undefined"，
    用户看到的就是"来源：undefined"——比没有来源还糟。
    """
    if ext == ".doc":
        pytest.skip("`.doc` 需要 LibreOffice，本机无 soffice")

    path = tmp_path / f"s{ext}"
    path.write_bytes(sample_bytes(ext))
    result = router.parse(path, f"xfm-{ext.lstrip('.')}", "default", {})

    for source, _ in result.segments:
        assert source.strip(), f"[{ext}] source 为空"
        assert "undefined" not in source.lower(), f"[{ext}] source 含 undefined：{source!r}"
        assert source.strip().lower() != "none", f"[{ext}] source 是 None：{source!r}"


# --------------------------------------------------------------------------
# XFM-09 50MB 边界
# --------------------------------------------------------------------------


def test_xfm09_size_limit_boundary() -> None:
    """XFM-09：超限文件被拒且**不留残留**。

    这里用 1MB 的限额做边界（而不是真造 50MB 文件）——
    判定逻辑在 `stream_to_disk` 里，与限额数值无关；
    真造 50MB × 2 只是让 CI 多花几秒 I/O，不增加任何判定力。
    限额默认值本身另有一条断言守着。

    协程放到**独立线程**里跑：`asyncio.run()` 不能在已有事件循环中调用，
    而全量套件里 pytest-asyncio 会在 session 级留一个循环，直接 asyncio.run
    会抛 "cannot be called from a running event loop"——那条失败与本用例
    断言的内容毫无关系，纯属执行环境冲突。
    """
    import asyncio
    import concurrent.futures

    from app.api.upload_util import stream_to_disk

    class _Upload:
        def __init__(self, data: bytes, name: str):
            self._data = data
            self.filename = name

        async def read(self, n: int) -> bytes:
            chunk = self._data[:n]
            self._data = self._data[n:]
            return chunk

    async def _run() -> None:
        dest = Path(tempfile.mkdtemp()) / "ok.bin"
        size = await stream_to_disk(_Upload(b"a" * (900 * 1024), "ok.bin"), dest, max_mb=1)
        assert size == 900 * 1024
        assert dest.exists(), "限额内的文件应保留"

        dest2 = Path(tempfile.mkdtemp()) / "too-big.bin"
        with pytest.raises(ApiError) as exc:
            await stream_to_disk(_Upload(b"a" * (1100 * 1024), "too-big.bin"), dest2, max_mb=1)
        assert exc.value.code == 2004, f"期望 2004，实际 {exc.value.code}"
        assert not dest2.exists(), "超限后**残留**了半成品文件"

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(asyncio.run, _run()).result()


def test_xfm09_default_limit_is_50mb() -> None:
    """XFM-09 补充：默认限额是 50MB（矩阵 §1.3）。"""
    from app.core.config import Settings

    assert Settings().max_upload_mb == 50, "默认单文件限额不再是 50MB"


# --------------------------------------------------------------------------
# XFM-10 同批重名
# --------------------------------------------------------------------------


def test_xfm10_duplicate_names_rejected(client: TestClient) -> None:
    """XFM-10：同批同名字 → 明确拒绝，不静默覆盖。"""
    resp = client.post(
        "/knowledge/ingest",
        files=[
            ("files", ("same.md", "# A\n\n内容甲\n".encode(), "text/markdown")),
            ("files", ("same.md", "# B\n\n内容乙\n".encode(), "text/markdown")),
        ],
    )
    assert resp.status_code == 400, f"期望 400，实际 {resp.status_code}"
    body = resp.json()
    assert body["error"]["code"] == 2001, f"期望 2001，实际 {body['error']['code']}"
    assert "同名" in body["error"]["message"], f"文案异常：{body['error']['message']}"


def test_xfm10_batch_size_limit(client: TestClient) -> None:
    """XFM-10 补充：单批最多 20 个文件（第 21 个被拒）。"""
    files = [
        ("files", (f"f{i}.md", f"# 文档{i}\n\n内容\n".encode(), "text/markdown"))
        for i in range(21)
    ]
    resp = client.post("/knowledge/ingest", files=files)
    assert resp.status_code == 400, f"期望 400，实际 {resp.status_code}"
    assert "20" in resp.json()["error"]["message"], (
        f"文案应说明上限：{resp.json()['error']['message']}"
    )
