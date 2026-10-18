"""阶段 7：GraphRAG 与多模态测试。

覆盖：抽取解析（坏 JSON/非法类型/空结果）/ 实体消歧（精确/别名/阈值/同类型硬约束）/
图存储 BFS（方向/深度/强度剪枝/按块删除）/ 社区编号稳定性 / 图检索种子策略 /
图像索引（去重/检索/元数据）/ API 契约。
"""

import numpy as np
import pytest

from app.domain.schemas import Chunk, EntityNode, RelationEdge
from app.services.graph.community import ConnectedComponentsDetector, build_communities
from app.services.graph.extractor import parse_extraction
from app.services.graph.images import ImageIndex, MockCaptioner, _parse_caption
from app.services.graph.query import GraphQueryService
from app.services.graph.resolver import EntityResolver, normalize_name
from app.services.graph.store import InMemoryGraphStore


class FakeEmbeddings:
    """确定性向量：语义相近的词给相近向量（消歧阈值测试用）。"""

    def __init__(self):
        self._table: dict[str, list[float]] = {}

    def set(self, name: str, vec: list[float]) -> None:
        v = np.array(vec, dtype=np.float32)
        self._table[name] = (v / np.linalg.norm(v)).tolist()

    def _vec(self, text: str) -> list[float]:
        if text in self._table:
            return self._table[text]
        # 默认：hash 派生零均值高维单位向量（standard_normal 各向同性，互相近正交）
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        v = rng.standard_normal(64).astype(np.float32)
        return (v / np.linalg.norm(v)).tolist()

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


def make_chunks() -> list[Chunk]:
    return [
        Chunk(chunk_id="doc#0", doc_id="doc", seq=0, content="Acme 公司投资了 Beta 创业公司。",
              source="第1页", tenant_id="default"),
        Chunk(chunk_id="doc#1", doc_id="doc", seq=1, content="Beta 创业公司的 CEO 是张三。",
              source="第1页", tenant_id="default"),
    ]


EXTRACT_RESULT = {
    "entities": [
        {"name": "Acme 公司", "type": "Org", "description": "投资公司"},
        {"name": "Beta 创业公司", "type": "Org", "description": "被投企业"},
        {"name": "张三", "type": "Person", "description": "CEO"},
    ],
    "relations": [
        {"source": "Acme 公司", "target": "Beta 创业公司", "relation": "投资",
         "evidence": "Acme 公司投资了 Beta 创业公司。"},
        {"source": "张三", "target": "Beta 创业公司", "relation": "任职",
         "evidence": "Beta 创业公司的 CEO 是张三。"},
    ],
}


class TestExtractionParsing:
    def test_parse_valid(self):
        raw = '前缀文字 {"entities": [{"name": "甲", "type": "Org", "description": ""}], "relations": [{"source": "甲", "target": "乙", "relation": "投资", "evidence": "甲投资乙"}]} 后缀'
        result = parse_extraction(raw)
        assert result["entities"][0]["name"] == "甲"
        assert result["relations"][0]["relation"] == "投资"

    def test_illegal_type_falls_back_to_concept(self):
        raw = '{"entities": [{"name": "甲", "type": "Alien", "description": ""}], "relations": []}'
        assert parse_extraction(raw)["entities"][0]["type"] == "Concept"

    def test_self_loop_and_empty_dropped(self):
        raw = '{"entities": [], "relations": [{"source": "甲", "target": "甲", "relation": "关联", "evidence": ""}, {"source": "", "target": "乙", "relation": "关联", "evidence": ""}]}'
        assert parse_extraction(raw)["relations"] == []

    def test_bad_json_raises(self):
        with pytest.raises(Exception):
            parse_extraction("完全不是 JSON")


class TestEntityResolver:
    def test_exact_and_alias_merge(self):
        resolver = EntityResolver(FakeEmbeddings(), sim_threshold=0.85)
        name1, new1 = resolver.resolve("Acme 公司", "Org")
        name2, new2 = resolver.resolve("Acme 公司", "Org")  # 规范化精确命中
        assert new1 and not new2
        assert name1 == name2
        assert resolver.lookup("acme公司") == name1  # 规范化名查别名

    def test_vector_merge_same_type(self):
        emb = FakeEmbeddings()
        emb.set("Acme Inc.", [1, 0, 0, 0, 0, 0, 0, 0])
        emb.set("Acme 公司", [0.98, 0.02, 0, 0, 0, 0, 0, 0])  # 相似度 ~0.99
        resolver = EntityResolver(emb, sim_threshold=0.85)
        resolver.resolve("Acme Inc.", "Org")
        canonical, is_new = resolver.resolve("Acme 公司", "Org")
        assert not is_new and canonical == "Acme Inc."
        assert "Acme 公司" in resolver.all_entities()[0]["aliases"]

    def test_same_name_different_type_not_merged(self):
        """硬约束：同名不同类型不合并（苹果公司 vs 苹果产品）。"""
        emb = FakeEmbeddings()
        emb.set("苹果", [1, 0, 0, 0, 0, 0, 0, 0])
        resolver = EntityResolver(emb, sim_threshold=0.5)  # 低阈值也不许跨类型
        resolver.resolve("苹果", "Org")
        _, is_new = resolver.resolve("苹果", "Product")
        assert is_new  # 不同类型 → 新建节点

    def test_below_threshold_not_merged(self):
        emb = FakeEmbeddings()
        emb.set("甲公司", [1, 0, 0, 0, 0, 0, 0, 0])
        emb.set("乙公司", [0, 1, 0, 0, 0, 0, 0, 0])  # 正交 → sim=0
        resolver = EntityResolver(emb, sim_threshold=0.85)
        resolver.resolve("甲公司", "Org")
        _, is_new = resolver.resolve("乙公司", "Org")
        assert is_new


class TestGraphStore:
    def _store_with_data(self) -> InMemoryGraphStore:
        store = InMemoryGraphStore()
        for name, t in [("Acme", "Org"), ("Beta", "Org"), ("张三", "Person"), ("Gamma", "Org")]:
            store.upsert_entity(EntityNode(entity_id=name, name=name, type=t))
        store.upsert_relation(RelationEdge(source="Acme", target="Beta", relation="投资",
                                           description="Acme 投资 Beta", source_chunks=["doc#0"]))
        store.upsert_relation(RelationEdge(source="张三", target="Beta", relation="任职",
                                           source_chunks=["doc#1"]))
        store.upsert_relation(RelationEdge(source="Beta", target="Gamma", relation="收购",
                                           source_chunks=["doc#2"]))
        return store

    def test_bfs_depth_and_direction(self):
        store = self._store_with_data()
        d1 = store.neighbors("Acme", depth=1)
        assert len(d1) == 1 and d1[0]["relation"] == "投资" and d1[0]["direction"] == "forward"
        d2 = store.neighbors("Acme", depth=2)
        # 深度 2：Acme→Beta→{张三(入), Gamma(出)}
        others = {(p["from"], p["to"]) for p in d2}
        assert ("Beta", "Gamma") in others
        assert ("张三", "Beta") in others

    def test_relation_direction_preserved(self):
        """方向是硬约束：A 投资 B ≠ B 投资 A。"""
        store = self._store_with_data()
        beta_view = store.neighbors("Beta", depth=1)
        acme_edge = next(p for p in beta_view if p["from"] == "Acme")
        assert acme_edge["direction"] == "backward"  # 从 Beta 视角是入边
        assert acme_edge["from"] == "Acme" and acme_edge["to"] == "Beta"

    def test_same_relation_merges_chunks_and_weight(self):
        store = self._store_with_data()
        store.upsert_relation(RelationEdge(source="Acme", target="Beta", relation="投资",
                                           source_chunks=["doc#9"]))
        rel = next(r for r in store.all_relations() if r.relation == "投资")
        assert set(rel.source_chunks) == {"doc#0", "doc#9"}
        assert rel.weight > 1.0

    def test_delete_by_chunk(self):
        """以块为最小更新单位：删除块后其独占关系消失。"""
        store = self._store_with_data()
        removed = store.delete_doc_chunks({"doc#0"})
        assert removed == 1
        assert all(r.relation != "投资" for r in store.all_relations())

    def test_persistence_roundtrip(self, tmp_path):
        path = tmp_path / "graph.json"
        store = self._store_with_data()
        store._path = path
        store.save()
        loaded = InMemoryGraphStore(path)
        assert loaded.stats()["entities"] == 4
        assert loaded.stats()["relations"] == 3


class TestCommunity:
    def test_stable_numbering(self):
        """同一图两次检测编号一致（否则摘要挂错社区）。"""
        store1 = TestGraphStore()._store_with_data()
        store2 = TestGraphStore()._store_with_data()
        g1 = ConnectedComponentsDetector().detect(store1)
        g2 = ConnectedComponentsDetector().detect(store2)
        assert g1 == g2 == [["Acme", "Beta", "Gamma", "张三"]]

    def test_summary_attached(self):
        store = TestGraphStore()._store_with_data()

        class FakeLLM:
            def generate(self, prompt: str) -> str:
                return "该社区围绕 Acme 的投资与收购活动"

            def stream(self, prompt):
                yield "x"

        communities = build_communities(store, FakeLLM(), min_size=2)
        assert len(communities) == 1
        assert communities[0].community_id == "c-001"
        assert "Acme" in communities[0].summary
        assert store.get_entity("Acme").community_id == "c-001"


class TestGraphQuery:
    def _service(self):
        store = TestGraphStore()._store_with_data()
        resolver = EntityResolver(FakeEmbeddings())
        for name, t in [("Acme", "Org"), ("Beta", "Org"), ("张三", "Person"), ("Gamma", "Org")]:
            resolver._entities[name] = {
                "name": name, "type": t, "description": "", "aliases": [],
                "vector": np.ones(8, dtype=np.float32),
            }
            resolver._alias_map[normalize_name(name)] = name
        return GraphQueryService(store, resolver, FakeEmbeddings())

    def test_seed_exact_match_and_paths(self):
        svc = self._service()
        result = svc.search("Acme 投资了哪些公司？")
        assert "Acme" in result["seeds"]
        assert any("Acme —投资→ Beta" in e["content"] for e in result["evidence_items"])
        assert all(e["modality"] == "graph" for e in result["evidence_items"])

    def test_no_seed_returns_empty_not_error(self):
        """实体未命中 → 空结果（上层回退向量检索，不判无答案）。"""
        svc = self._service()
        result = svc.search("完全无关的问题 xyzzy")
        assert result["seeds"] == [] and result["evidence_items"] == []


class TestImageIndex:
    def test_caption_dedupe_and_search(self, tmp_path):
        from app.services.vectorstore import VectorStore

        store = VectorStore(tmp_path / "vec", dim=64)
        index = ImageIndex(MockCaptioner(), FakeEmbeddings(), store, tmp_path / "img.json")
        img = tmp_path / "battery.png"
        img.write_bytes(b"\x89PNG fake image bytes")
        e1 = index.index_image(img, "doc#img-1", doc_id="doc", page=12)
        e2 = index.index_image(img, "doc#img-2", doc_id="doc", page=13)  # 同图同 hash → 去重
        assert e1 is not None and e2 is e1
        assert len(index._entries) == 1
        assert e1.model_version  # 记录描述模型版本
        results = index.search("battery 示意图")
        assert results and results[0]["page"] == 12
        assert "主体" in results[0]["caption"]

    def test_caption_fence_stripped(self):
        assert _parse_caption("```markdown\n主体：猫\n```") == "主体：猫"


class TestGraphApi:
    def test_build_and_query_flow(self, client):
        """API 端到端：上传 → 建图（mock 模式 ScriptedExtractor 空结果）→ 接口契约。"""
        # mock 模式 extractor 为空 → 空图谱
        resp = client.get("/graph/entities")
        assert resp.status_code == 200
        assert resp.json()["data"] == []
        # 子图默认视图
        sub = client.get("/graph/subgraph").json()["data"]
        assert sub["nodes"] == [] and sub["truncated"] is False
        # 实体不存在 → 3401
        resp = client.get("/graph/entity/不存在")
        assert resp.json()["error"]["code"] == 3401
        # 社区与统计
        assert client.get("/graph/communities").json()["data"] == []
        stats = client.get("/graph/stats").json()["data"]
        assert stats["entities"] == 0 and "orphan_ratio" in stats and "images" in stats
        # 以文搜图（空库）
        resp = client.post("/search/media", json={"query": "示意图"})
        assert resp.json()["data"]["items"] == []
        # assets 404
        assert client.get("/assets/not-exist.png").status_code == 404

    def test_build_endpoint_contract(self, client):
        md = "# 测试\n\nAcme 公司投资了 Beta 创业公司。Beta 的 CEO 是张三。\n"
        client.post("/knowledge/upload", files={"file": ("公司.md", md.encode(), "text/markdown")})
        resp = client.post("/graph/build", json={"doc_id": None, "rebuild_communities": True})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "chunks_processed" in data and "community" in data
        # 文档不存在 → 404
        resp = client.post("/graph/build", json={"doc_id": "not-exist"})
        assert resp.json()["error"]["code"] == 3002
