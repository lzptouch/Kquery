"""切块器：分隔符优先级、chunk_id 稳定性、空块过滤、超短块标记。"""
from app.services.chunking import build_chunks, split_text


def test_split_respects_chunk_size():
    text = "第一段内容，包含若干句子。" * 30
    chunks = split_text(text, chunk_size=300, overlap=50)
    assert len(chunks) > 1
    assert all(len(c) <= 300 for c in chunks)


def test_split_prefers_paragraph_boundary():
    text = "甲段落很短。\n\n乙段落也很短。"
    chunks = split_text(text, chunk_size=300, overlap=50)
    assert chunks == ["甲段落很短。\n\n乙段落也很短。"]


def test_long_unbroken_string_is_hard_cut():
    text = "x" * 1000
    chunks = split_text(text, chunk_size=300, overlap=50)
    assert len(chunks) >= 3
    assert all(len(c) <= 300 for c in chunks)


def test_overlap_between_adjacent_chunks():
    text = "无标点长文本" * 100
    chunks = split_text(text, chunk_size=300, overlap=50)
    assert len(chunks) > 1
    # 相邻块共享尾部内容（重叠保留跨段语义）
    assert chunks[0][-20:] in chunks[1] or chunks[1][:20] in chunks[0]


def test_chunk_id_is_stable_and_sequential():
    segments = [("全文", "内容甲。\n\n内容乙。" * 10)]
    first = build_chunks("doc-a", segments, tenant_id="t", chunk_size=100, overlap=20)
    second = build_chunks("doc-a", segments, tenant_id="t", chunk_size=100, overlap=20)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert [c.chunk_id for c in first] == [f"doc-a#{i}" for i in range(1, len(first) + 1)]


def test_empty_and_short_chunks():
    # 长段落超过 chunk_size，"短。" 无法被合并进去 → 独立成块并被标记
    segments = [("全文", "短。\n\n" + "长" * 500)]
    chunks = build_chunks("doc-b", segments, tenant_id="t", chunk_size=300, overlap=50, min_chars=20)
    assert all(c.content.strip() for c in chunks)
    assert any(c.is_short for c in chunks)


def test_no_empty_chunks_produced():
    segments = [("全文", "\n\n\n\n")]
    assert build_chunks("doc-c", segments, tenant_id="t") == []
