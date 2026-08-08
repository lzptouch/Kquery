from pathlib import Path

import pytest

from app.services.chunking import chunk_text


@pytest.mark.unit
class TestChunkText:
    def test_chunk_by_size(self) -> None:
        text = "a " * 200
        chunks = chunk_text(text, chunk_size=50, overlap=10)
        assert len(chunks) > 1
        total = sum(len(c) for c in chunks)
        assert total > 0

    def test_overlap_between_chunks(self) -> None:
        text = "段落1。段落2。段落3。段落4。段落5。"
        chunks = chunk_text(text, chunk_size=20, overlap=5)
        if len(chunks) > 1:
            # 重叠部分：第二个 chunk 的前 5 个字符应出现在第一个 chunk 末尾附近
            assert chunks[1][:5] in chunks[0]

    def test_short_text_single_chunk(self) -> None:
        chunks = chunk_text("hello world", chunk_size=100, overlap=10)
        assert len(chunks) == 1
        assert chunks[0] == "hello world"

    def test_empty_text(self) -> None:
        chunks = chunk_text("", chunk_size=100, overlap=10)
        assert chunks == []

    def test_chunk_contains_original_text(self) -> None:
        text = "line1\nline2\nline3"
        chunks = chunk_text(text, chunk_size=10, overlap=0)
        joined = " ".join(chunks)
        # 分块后可能去除换行，断言原始内容被保留
        assert "line1" in joined and "line2" in joined and "line3" in joined
