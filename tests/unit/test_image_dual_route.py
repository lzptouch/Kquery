"""M1：图像检索双路（caption 路 + 图像向量路）与 RRF 融合。"""
from pathlib import Path


def _index(tmp: Path, with_image_route: bool):
    from app.services.embeddings import MockEmbeddings
    from app.services.graph.image_embedder import MockImageEmbedder
    from app.services.graph.images import ImageIndex, MockCaptioner
    from app.services.vectorstore import VectorStore

    assets = tmp / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    img = assets / "fig.png"
    img.write_bytes(b"fake-image-bytes")

    store = VectorStore(tmp / "text-index", 1024)
    image_store = VectorStore(tmp / "image-index", 1024) if with_image_route else None
    embedder = MockImageEmbedder(dim=1024) if with_image_route else None
    index = ImageIndex(
        MockCaptioner(), MockEmbeddings(dim=1024), store, tmp / "images.json",
        image_embedder=embedder, image_store=image_store,
    )
    return index, store, image_store, img


class TestDualRoute:
    def test_caption_only_keeps_old_behaviour(self):
        """没配图像向量路 → 单路，行为与改造前一致。"""
        import tempfile as tf

        with tf.TemporaryDirectory() as d:
            tmp = Path(d)
            index, _store, _is, img = _index(tmp, with_image_route=False)
            index.index_image(img, "doc#page-1", doc_id="doc")
            hits = index.search("流程图", top_k=3)
            assert len(hits) == 1
            assert hits[0]["source_route"] == "image_caption"

    def test_dual_route_queries_both_stores(self):
        """双路都要真的被查（否则"配了图像路但没生效"这种假实现会溜过去）。

        mock 编码器下文本哈希与图像哈希不同 → 图像路**查不到**是正常的，
        本用例只断言"两路都被调用了 + 结果仍可用"。
        """
        import tempfile as tf

        with tf.TemporaryDirectory() as d:
            tmp = Path(d)
            index, store, image_store, img = _index(tmp, with_image_route=True)
            index.index_image(img, "doc#page-1", doc_id="doc")

            calls = {"text": 0, "image": 0}
            orig_text, orig_image = store.search, image_store.search

            def spy_text(*a, **kw):
                calls["text"] += 1
                return orig_text(*a, **kw)

            def spy_image(*a, **kw):
                calls["image"] += 1
                return orig_image(*a, **kw)

            store.search = spy_text
            image_store.search = spy_image
            hits = index.search("流程图", top_k=3)
            assert calls["text"] == 1 and calls["image"] == 1, "两路都必须被查询"
            assert len(hits) == 1, "图像路无命中时应回落 caption 路的结果"

    def test_image_route_failure_falls_back(self):
        """图像路抛错 → 回落 caption 路（不能整个搜索失败）。"""
        import tempfile as tf

        with tf.TemporaryDirectory() as d:
            tmp = Path(d)
            index, _store, _is, img = _index(tmp, with_image_route=True)
            index.index_image(img, "doc#page-1", doc_id="doc")

            class Boom:
                def embed_query(self, text):
                    raise RuntimeError("图像路挂了")

            index._image_embedder = Boom()
            hits = index.search("流程图", top_k=3)
            assert len(hits) == 1
            assert hits[0]["source_route"] == "image_caption"

    def test_rrf_prefers_items_hit_by_both_routes(self):
        """两路都命中的排在只命中一路的前面（RRF 的意义）。"""
        from app.services.graph.images import ImageIndex

        caption = [{"asset_id": "a", "source_route": "image_caption", "score": 0.1}]
        image = [{"asset_id": "a", "source_route": "image_vector", "score": 0.9}]
        only_caption = [{"asset_id": "b", "source_route": "image_caption", "score": 0.99}]
        fused = ImageIndex._fuse(caption + only_caption, image)
        assert fused[0]["asset_id"] == "a"
        assert "image_caption" in fused[0]["source_route"]
        assert "image_vector" in fused[0]["source_route"]
