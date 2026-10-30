"""通用解析契约断言器（矩阵 §二 的 C1~C8 + 扩展契约 C9~C12）。

为什么要有断言器而不是每个用例各写各的：
格式专项的价值在于"**每种格式都必须过同一套判定**"。如果每个用例自己写断言，
新加一个格式时很容易只测"能解析出来"——而"能解析出来"恰恰是最弱的那条：
解析器返回空结果、不给 warning、表格被切碎，用户拿到的都是"入库成功"，
实际什么都搜不到。所以契约被收敛到一个函数里，**每种格式都必须整份通过**。

C1（路由命中）与 C2（白名单一致）属于"入口"性质，在 `test_all_extensions.py`
里逐扩展名断言；这里放的是"产物"性质的 C3~C7 与扩展契约 C9~C12。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.domain.schemas import DocumentRecord
from app.services.chunkers.factory import apply_chunk_policy
from app.services.embeddings import MockEmbeddings
from app.services.knowledge import TABLE_SOURCE_MARK, KnowledgeService
from app.services.parsers.base import ParseResult

#: 仓库根（backend/ 的上一级）
REPO_ROOT = Path(__file__).resolve().parents[3]

#: 前端白名单真源
FRONTEND_FILE_TYPES = REPO_ROOT / "frontend" / "src" / "lib" / "file-types.ts"

#: warning 的可识别关键词（矩阵 C7：warning 全部可见）
WARNING_KEYWORDS = (
    "截断", "超限", "上限", "为空", "空", "扫描页", "OCR", "异常", "已截断",
    "超过", "未解析到", "降级", "替换字符",
)


def frontend_allowed_extensions() -> set[str]:
    """从前端 `file-types.ts` 解析出白名单。

    直接读源码而不是维护一份副本——副本必然会漂移，而漂移正是 XFM-01 要抓的。
    """
    source = FRONTEND_FILE_TYPES.read_text(encoding="utf-8")
    # 只取 ALLOWED_EXTENSIONS 之前的四个分组常量，避免把 UNSUPPORTED_HINTS 里的
    # ".xls"/".ppt" 也算进来（那两个是"不支持但给提示"的扩展名）。
    head = source.split("export const ALLOWED_EXTENSIONS")[0]
    return set(re.findall(r'"(\.[a-z0-9]+)"', head))


def chunk_like_production(
    doc_id: str,
    parsed: ParseResult,
    tenant_id: str = "default",
    settings=None,
):
    """走**生产同一条**切块路径（`KnowledgeService.apply_chunking`）。

    刻意不自己重写一遍"表格段分流 + 表格块 modality=table"的逻辑：
    表格能不能独立成块，取决于 `source` 里有没有 `·表格` 标记，
    而这个标记是入库时才识别的。重写一遍等于测的是"我以为的生产逻辑"。
    这里直接调生产代码，测的就是真的那条路。
    """
    from app.core.config import Settings

    settings = settings or Settings()
    record = DocumentRecord(
        doc_id=doc_id,
        file_id=f"{doc_id}-f1",
        filename=f"{doc_id}.bin",
        tenant_id=tenant_id,
        source_path=f"/tmp/{doc_id}.bin",
        content_hash="h",
    )
    service = KnowledgeService(
        settings=settings,
        embeddings=MockEmbeddings(),
        llm=None,  # recursive 策略用不到 LLM
        store=None,
        bm25=None,
        docstore=None,
        parser_router=None,
    )
    return service.apply_chunking(record, parsed, {"strategy": "recursive", "params": {}})


def text_chunks(doc_id: str, segments, tenant_id: str = "default"):
    """纯文本切块（不经 KnowledgeService，供只想看文本块时的轻量断言用）。"""
    return apply_chunk_policy(
        doc_id, segments, tenant_id, {"strategy": "recursive", "params": {}}, None, None
    ).chunks


def assert_parse_contract(
    result: ParseResult,
    fmt: str,
    *,
    expect_tables: bool = False,
    expect_assets: bool = False,
) -> None:
    """断言一份解析产物满足通用契约（C3~C7）。

    参数 `fmt` 只进错误信息，方便失败时一眼看出是哪种格式不合规。
    """
    # ---- C3 产出非空：空产出 = 静默半成功 ----
    assert result.segments or result.tables, (
        f"[{fmt}] C3 违反：解析产出为空（segments 与 tables 都空）。"
        "用户会看到'入库成功'但搜不到任何内容。"
    )

    # ---- C4 段 source 命名规范：非空、可定位 ----
    for source, text in result.segments:
        assert source and source.strip(), (
            f"[{fmt}] C4 违反：存在 source 为空的段——前端溯源会显示空白"
        )
        assert "undefined" not in source and "None" not in source, (
            f"[{fmt}] C4 违反：source 含未渲染的占位符：{source!r}"
        )
        assert text and text.strip(), f"[{fmt}] C4 违反：存在空文本段（source={source!r}）"

    # ---- C5 表格独立成块：表格不被切碎 ----
    if expect_tables:
        assert result.tables, f"[{fmt}] C5 违反：期望有表格但 tables 为空"
        for table in result.tables:
            assert table.rows >= 2 and table.cols >= 2, (
                f"[{fmt}] C5 违反：表格 {table.table_id} 行列数 "
                f"{table.rows}x{table.cols}，单行单列表格应被丢弃"
            )
            assert table.markdown.strip(), f"[{fmt}] C5 违反：表格 {table.table_id} markdown 为空"

        # 表格必须能被切块层识别出来（source 带 ·表格 标记）→ modality=="table"
        table_segments = [s for s in result.segments if TABLE_SOURCE_MARK in s[0]]
        assert table_segments, (
            f"[{fmt}] C5 违反：tables 有记录但没有任何段带「{TABLE_SOURCE_MARK}」标记，"
            "表格会在入库时被当普通文本切碎"
        )
        chunks = chunk_like_production(f"{fmt}-c5", result)
        table_chunks = [c for c in chunks if c.modality == "table"]
        assert table_chunks, (
            f"[{fmt}] C5 违反：切块后没有 modality=='table' 的块"
        )
        for chunk in table_chunks:
            assert chunk.chunker == "table", (
                f"[{fmt}] C5 违反：表格块的 chunker 应为 'table'，实际 {chunk.chunker!r}"
            )

        # ---- C6 表格不混入正文 ----
        table_markdowns = [t.markdown.strip() for t in result.tables]
        for source, text in result.segments:
            if TABLE_SOURCE_MARK in source:
                continue
            for markdown in table_markdowns:
                # 正文段里不应出现表格 markdown 的首行（表头）
                first_line = markdown.splitlines()[0].strip()
                if len(first_line) > 4:
                    assert first_line not in text, (
                        f"[{fmt}] C6 违反：正文段（source={source!r}）里混进了表格表头 {first_line!r}"
                    )

    # ---- C7 warning 全部可见 ----
    for warning in result.warnings:
        assert warning and warning.strip(), f"[{fmt}] C7 违反：存在空 warning"
        assert any(k in warning for k in WARNING_KEYWORDS), (
            f"[{fmt}] C7 违反：warning 不含任何可识别关键词，用户看不懂发生了什么：{warning!r}"
        )

    # ---- 资源（有则断言可定位）----
    if expect_assets:
        assert result.assets, f"[{fmt}] 期望有资源但 assets 为空"
    for asset in result.assets:
        assert asset.uri and asset.uri.strip(), f"[{fmt}] 资源 uri 为空：{asset}"
        assert asset.kind, f"[{fmt}] 资源 kind 为空：{asset}"


def assert_idempotent(parse_fn, *, times: int = 2):
    """C12 幂等性：同一文件解析多次 → segments 数量与内容一致。"""
    results = [parse_fn() for _ in range(times)]
    first = results[0]
    for other in results[1:]:
        assert len(other.segments) == len(first.segments), (
            f"C12 违反：segments 数量不一致（{len(first.segments)} vs {len(other.segments)}）"
        )
        assert other.segments == first.segments, "C12 违反：segments 内容不一致"
        assert len(other.tables) == len(first.tables), (
            f"C12 违反：tables 数量不一致（{len(first.tables)} vs {len(other.tables)}）"
        )
    return first
