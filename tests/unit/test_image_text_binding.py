"""M4 图文绑定（图像 ↔ 邻近文本块）的测试。

**为什么这条链路值得测**：绑定错了不会报错——只是"答案旁边的图不对"，
用户看到的是一张毫不相干的插图，比没有图更糟。
"""

from app.domain.schemas import Chunk
from app.services.parsers.base import ParseResult


def _chunk(cid: str, source: str, doc_id: str = "doc1") -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=doc_id,
        seq=1,
        content="正文",
        source=source,
        tenant_id="t1",
    )


class TestBindRelatedAssets:
    def test_page_hint_binds_to_same_page(self):
        from app.services.knowledge import _bind_related_assets

        chunks = [
            _chunk("doc1#1", "第1页"),
            _chunk("doc1#2", "第2页"),
            _chunk("doc1#3", "第3页"),
        ]
        parsed = ParseResult(asset_bindings=[("doc1#page-2", "第2页")])
        assert _bind_related_assets(chunks, parsed) == 1
        assert chunks[0].related_assets == []
        assert chunks[1].related_assets == ["doc1#page-2"]
        assert chunks[2].related_assets == []

    def test_section_hint_binds_to_section(self):
        """DOCX：章节标题 → 该章节的块。"""
        from app.services.knowledge import _bind_related_assets

        chunks = [
            _chunk("doc1#1", "系统架构"),
            _chunk("doc1#2", "系统架构"),
            _chunk("doc1#3", "部署运维"),
        ]
        parsed = ParseResult(asset_bindings=[("doc1#img-1", "系统架构")])
        assert _bind_related_assets(chunks, parsed) == 2
        assert chunks[0].related_assets == ["doc1#img-1"]
        assert chunks[1].related_assets == ["doc1#img-1"]
        assert chunks[2].related_assets == []

    def test_empty_hint_binds_to_all_chunks(self):
        """没有提示 → 绑到全部块（宁可多绑也不要丢图）。"""
        from app.services.knowledge import _bind_related_assets

        chunks = [_chunk("doc1#1", "第1页"), _chunk("doc1#2", "第2页")]
        parsed = ParseResult(asset_bindings=[("doc1#page-1", "")])
        assert _bind_related_assets(chunks, parsed) == 2
        assert all(c.related_assets for c in chunks)

    def test_no_bindings_is_noop(self):
        from app.services.knowledge import _bind_related_assets

        chunks = [_chunk("doc1#1", "第1页")]
        assert _bind_related_assets(chunks, ParseResult()) == 0
        assert chunks[0].related_assets == []

    def test_same_asset_not_duplicated(self):
        from app.services.knowledge import _bind_related_assets

        chunks = [_chunk("doc1#1", "第1页")]
        parsed = ParseResult(
            asset_bindings=[("doc1#page-1", "第1页"), ("doc1#page-1", "第1页")]
        )
        assert _bind_related_assets(chunks, parsed) == 1
        assert chunks[0].related_assets == ["doc1#page-1"]


class TestAssetUri:
    def test_page_asset_uri(self):
        from app.services.pipeline import _asset_uri

        chunk = _chunk("doc1#1", "第2页")
        chunk.related_assets = ["doc1#page-2"]
        assert _asset_uri(chunk) == "doc1#page-2.png"

    def test_inline_image_uri(self):
        from app.services.pipeline import _asset_uri

        chunk = _chunk("doc1#1", "章节")
        chunk.related_assets = ["doc1#img-3"]
        assert _asset_uri(chunk) == "doc1-img3"

    def test_no_assets_empty_uri(self):
        from app.services.pipeline import _asset_uri

        assert _asset_uri(_chunk("doc1#1", "第1页")) == ""


class TestParseResultBinding:
    def test_parse_result_carries_bindings(self):
        parsed = ParseResult(asset_bindings=[("a#page-1", "第1页")])
        assert parsed.asset_bindings == [("a#page-1", "第1页")]
