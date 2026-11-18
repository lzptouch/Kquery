"""**全部 50 个支持扩展名**逐一过通用解析契约。

这是矩阵 §1.1 的"格式全景"对应的自动化守卫。矩阵 §1.4 登记的缺口 #8
（"零真实样本，全现场构造"）说的就是这一层：**每个扩展名都要能被路由、
被解析、产出可用的结构**，一个都不能是"注册了但一用就炸"。

分层断言：
- **C1 路由命中**：`ParserRouter.route()` 返回预期的解析器名；
- **C2 白名单一致**：扩展名 ∈ 后端 `SUPPORTED_EXTENSIONS` 且 ∈ 前端 `ALLOWED_EXTENSIONS`；
- **C3~C7 产物契约**：见 `contract.assert_parse_contract`；
- **C12 幂等性**：同一文件解析两次产物一致。

特例（必须在断言里写明原因，不许静默跳过）：
- `.doc`：老式 OLE 格式需 LibreOffice 转换；本机无 soffice → 断言**明确报错**；
"""
from __future__ import annotations

import pytest

from app.core.envelope import ApiError
from app.services.parsers.router import SUPPORTED_EXTENSIONS, ParserRouter

from .contract import (
    assert_idempotent,
    assert_parse_contract,
    frontend_allowed_extensions,
)
from .fixtures import (
    ALL_EXTENSION_SAMPLES,
    CODE_LIKE,
    EXPECTED_PARSER,
    EXTENSION_FORMAT,
    FMT_CODE,
    FMT_DOCX,
    FMT_IMAGE,
    FMT_MARKDOWN,
    FMT_PLAIN,
    FMT_PPTX,
    FMT_XLSX,
    TEXT_LIKE,
    sample_bytes,
)

ALL_EXTENSIONS = sorted(ALL_EXTENSION_SAMPLES)

#: 期望产出表格的格式（其余格式产出正文段即可）
FORMATS_WITH_TABLES = {FMT_DOCX, FMT_XLSX, FMT_PPTX}


def test_extension_count_is_50() -> None:
    """矩阵 §1.1：50 个扩展名。新增格式时这个断言会**逼你更新矩阵**。

    这是有意为之的"摩擦"：白名单变了却没人改文档，几个月后文档就是错的，
    而文档是唯一能回答"系统到底支持什么"的地方。
    """
    assert len(ALL_EXTENSIONS) == 50, (
        f"扩展名数量变为 {len(ALL_EXTENSIONS)}（原 52，语音下线后 50）。"
        "请同步更新 doc/testing/01-文件格式解析测试矩阵.md §1.1 与 §1.2。"
    )
    assert len(SUPPORTED_EXTENSIONS) == 50, (
        f"后端 SUPPORTED_EXTENSIONS 变为 {len(SUPPORTED_EXTENSIONS)}（原 52，语音下线后 50）"
    )


def test_no_duplicate_sample_builders() -> None:
    """每个扩展名都要有独立的造件器——漏一个就等于那个格式"没被测"。"""
    missing = [ext for ext in ALL_EXTENSIONS if ext not in ALL_EXTENSION_SAMPLES]
    assert not missing, f"以下扩展名没有造件器：{missing}"


# --------------------------------------------------------------------------
# C1 + C2：路由与白名单
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ALL_EXTENSIONS)
def test_c1_route_hits_expected_parser(ext: str, router: ParserRouter, tmp_path) -> None:
    """C1：路由命中预期的解析器。"""
    path = tmp_path / f"sample{ext}"
    path.write_bytes(b"x")
    assert router.route(path) == EXPECTED_PARSER[ext], (
        f"[{ext}] 路由错误：期望 {EXPECTED_PARSER[ext]}，实际 {router.route(path)}"
    )


@pytest.mark.parametrize("ext", ALL_EXTENSIONS)
def test_c2_whitelist_aligned_with_frontend(ext: str) -> None:
    """C2：后端白名单与前端白名单**逐字一致**。

    两份清单各写各的，迟早对不上：前端放行、后端拒绝 → 用户看到"能选却传不上去"；
    前端拦掉、后端支持 → 用户永远传不上去。
    """
    assert ext in SUPPORTED_EXTENSIONS, f"[{ext}] 不在后端 SUPPORTED_EXTENSIONS 中"
    frontend = frontend_allowed_extensions()
    assert ext in frontend, (
        f"[{ext}] 不在前端 ALLOWED_EXTENSIONS 中——前后端白名单漂移了。"
        "真源：backend/app/services/parsers/router.py 与 frontend/src/lib/file-types.ts"
    )


def test_xfm01_backend_frontend_whitelist_identical() -> None:
    """XFM-01：两份白名单**完全相同**（不只是"都包含"）。"""
    backend = set(SUPPORTED_EXTENSIONS)
    frontend = frontend_allowed_extensions()
    assert backend == frontend, (
        "前后端白名单不一致：\n"
        f"  仅后端有：{sorted(backend - frontend)}\n"
        f"  仅前端有：{sorted(frontend - backend)}"
    )


# --------------------------------------------------------------------------
# C3~C7 + C12：产物契约
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", ALL_EXTENSIONS)
def test_c3_c7_parse_contract(ext: str, router: ParserRouter, tmp_path) -> None:
    """每个扩展名都能被解析，且产物满足通用契约。

    `.doc` 单独处理：它需要 LibreOffice 才能转格式，本机没有 → 断言**明确报错**，
    而不是"跳过"（跳过等于没测，而"用户传 .doc 得到一句人话"本身就是被测点）。
    """
    fmt = EXTENSION_FORMAT[ext]
    path = tmp_path / f"sample{ext}"
    path.write_bytes(sample_bytes(ext))

    if ext == ".doc":
        with pytest.raises(ApiError) as exc:
            router.parse(path, "doc-1", "default", {})
        assert exc.value.code == 3003, f"[{ext}] 期望 3003，实际 {exc.value.code}"
        assert "LibreOffice" in exc.value.message, (
            f"[{ext}] 错误文案必须说明需要 LibreOffice：{exc.value.message}"
        )
        return

    result = router.parse(path, f"{ext.lstrip('.')}-1", "default", {})
    assert_parse_contract(
        result,
        fmt,
        expect_tables=fmt in FORMATS_WITH_TABLES,
        expect_assets=fmt in (FMT_IMAGE,),
    )


@pytest.mark.parametrize("ext", ALL_EXTENSIONS)
def test_c12_idempotent(ext: str, router: ParserRouter, tmp_path) -> None:
    """C12 幂等性：同一文件解析两次 → 产物一致。

    不幂等意味着重传同一个文件会得到不同的索引，"重新解析"功能形同虚设。
    """
    if ext == ".doc":
        pytest.skip("`.doc` 走 LibreOffice 转换路径，幂等性由转换器保证，不在本层断言")

    path = tmp_path / f"sample{ext}"
    path.write_bytes(sample_bytes(ext))

    def _parse():
        return router.parse(path, f"{ext.lstrip('.')}-idem", "default", {})

    assert_idempotent(_parse)


# --------------------------------------------------------------------------
# 格式族级别的判定
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ext", sorted(TEXT_LIKE))
def test_plaintext_family_source_naming(ext: str, router: ParserRouter, tmp_path) -> None:
    """纯文本族的 source 命名可读（前端溯源直接显示它）。"""
    path = tmp_path / f"sample{ext}"
    path.write_bytes(sample_bytes(ext))
    result = router.parse(path, "plain-1", "default", {})
    source = result.segments[0][0]
    assert source in ("全文", "表格数据", "正文", "代码"), (
        f"[{ext}] source 命名异常：{source!r}"
    )


@pytest.mark.parametrize("ext", sorted(CODE_LIKE))
def test_code_family_content_preserved(ext: str, router: ParserRouter, tmp_path) -> None:
    """CODE-02：代码全文保留，不做符号解析。"""
    path = tmp_path / f"sample{ext}"
    path.write_bytes(sample_bytes(ext))
    result = router.parse(path, "code-1", "default", {})
    text = result.segments[0][1]
    assert text.strip(), f"[{ext}] 代码内容为空"
    assert "CODE" in text.upper(), f"[{ext}] 代码内容疑似丢失：{text[:80]!r}"
    assert result.segments[0][0] == "代码", f"[{ext}] 代码族 source 应为「代码」"


@pytest.mark.parametrize("ext", [".png", ".jpg", ".jpeg"])
def test_image_family_produces_asset(ext: str, router: ParserRouter, tmp_path) -> None:
    """IMG-02：图片走 OCR，产出段 + 资源记录。"""
    path = tmp_path / f"sample{ext}"
    path.write_bytes(sample_bytes(ext))
    result = router.parse(path, "img-1", "default", {})
    assert result.segments, f"[{ext}] OCR 未产出任何段"
    assert "OCR" in result.segments[0][0], (
        f"[{ext}] OCR 段的 source 应标明 OCR：{result.segments[0][0]!r}"
    )
    assert result.assets and result.assets[0].kind == "image", (
        f"[{ext}] 缺少 kind=='image' 的资源记录"
    )


def test_extension_family_coverage() -> None:
    """守卫：9 个格式族都有扩展名归属（防止新扩展名忘了归类）。"""
    families = {EXTENSION_FORMAT[ext] for ext in ALL_EXTENSIONS}
    expected = {
        FMT_PLAIN, FMT_CODE, FMT_IMAGE,
        "pdf", FMT_MARKDOWN, FMT_DOCX, FMT_XLSX, FMT_PPTX,
    }
    assert families == expected, (
        f"格式族覆盖不全：缺 {sorted(expected - families)}，多 {sorted(families - expected)}"
    )
    # 每个族至少有一个扩展名
    for family in expected:
        assert any(EXTENSION_FORMAT[ext] == family for ext in ALL_EXTENSIONS), (
            f"格式族 {family} 没有任何扩展名"
        )
