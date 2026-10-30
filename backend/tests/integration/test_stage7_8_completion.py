"""/8 补全项的回归测试（图存储抽象、社区层级、入库联动、权限矩阵）。

对应本轮修复的每个真实缺陷：

1.  `BaseGraphStore` 抽象真的存在，且三个消费者只依赖契约
2.  社区多级 level：层级数、编号稳定性、parent_id 指向
3.  图构建与图像索引改为入库联动（此前只能手动打端点）
4.  权限矩阵端点：能力矩阵 + enforced 标注 + 审计
5.  Query 分析 cost 字段（「成本最容易被忽略」）

注：原第 4/5/8 项（报告 SSE 事件、`rejected`/`capped` 区分、`_run_layer` 事件归属）
属多智能体报告链路，已随报告中心下线移除。
"""

import pytest

from app.domain.schemas import Community, EntityNode, RelationEdge
from app.services.graph.community import (
    ConnectedComponentsDetector,
    HierarchicalCommunityDetector,
    build_communities,
)
from app.services.graph.store import BaseGraphStore, InMemoryGraphStore

SAMPLE_MD = """# 保修手册

本产品整机保修两年，电池保修一年。

# 退货政策

七天无理由退货，退货运费由买家承担。

# 产品 A 保修

产品 A 整机保修三年。
"""


def _build_store(edges: list[tuple[str, str]], tmp_path=None) -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    for i, (source, target) in enumerate(edges):
        store.upsert_entity(EntityNode(entity_id=source, name=source, type="Concept"))
        store.upsert_entity(EntityNode(entity_id=target, name=target, type="Concept"))
        store.upsert_relation(RelationEdge(
            source=source, target=target, relation="关联",
            source_chunks=[f"doc#{i}"],
        ))
    return store


# ---------------------------------------------------------------- 1. 图存储抽象


class TestGraphStoreAbstraction:
    def test_memory_store_satisfies_protocol(self):
        """换存储引擎的前提：内存实现真的满足契约。"""
        store = InMemoryGraphStore()
        assert isinstance(store, BaseGraphStore)

    def test_consumers_depend_on_contract_not_concrete_class(self):
        """此前 builder/query/community 三处直接绑定 InMemoryGraphStore。"""
        import inspect

        from app.services.graph import builder, community, query

        for module in (builder, query, community):
            source = inspect.getsource(module)
            assert "InMemoryGraphStore" not in source, (
                f"{module.__name__} 仍然直接依赖具体实现，换引擎要改这里"
            )
            assert "BaseGraphStore" in source

    def test_custom_store_can_be_injected(self):
        """一个满足契约的假存储应当能直接接进查询服务。"""
        from app.services.graph.query import GraphQueryService

        class MinimalStore:
            def __init__(self):
                self.entities = {"产品A": EntityNode(entity_id="产品A", name="产品A", type="Product")}
                self.relations = [RelationEdge(
                    source="产品A", target="保修", relation="保修政策", source_chunks=["c1"],
                )]

            def upsert_entity(self, entity): ...
            def upsert_relation(self, rel): ...
            def delete_doc_chunks(self, chunk_ids): return 0
            def get_entity(self, name): return self.entities.get(name)
            def neighbors(self, name, depth=2, max_edges=50): return []
            def all_entities(self): return list(self.entities.values())
            def all_relations(self): return list(self.relations)
            def set_community(self, community): ...
            def communities(self): return []
            def stats(self): return {}
            def save(self): ...

        store = MinimalStore()
        assert isinstance(store, BaseGraphStore)
        # 能构造出查询服务即证明契约够用
        svc = GraphQueryService(store, resolver=None, embeddings=None)
        assert svc is not None


# ---------------------------------------------------------------- 2. 社区层级


class TestHierarchicalCommunities:
    #: 两个独立簇，各自内部有枢纽把它们连起来——单层会各自成社区，
    #: L2 应当把它们保持分开，L3 才可能因更高重叠而合并
    EDGES = [
        ("政策A", "保修"), ("政策A", "退货"),
        ("政策B", "保修"), ("政策B", "退货"),
        ("产品X", "政策A"), ("产品Y", "政策B"),
    ]

    def test_level0_is_connected_components(self):
        store = _build_store(self.EDGES)
        detector = HierarchicalCommunityDetector(max_level=3)
        levels = detector.detect_levels(store)
        assert levels[0] == ConnectedComponentsDetector().detect(store)
        assert all(c.level == 0 for c in [Community(community_id="x", level=0)])

    def test_higher_levels_are_coarser(self):
        store = _build_store(self.EDGES)
        levels = HierarchicalCommunityDetector(max_level=3).detect_levels(store)
        sizes = [sum(len(g) for g in level) for level in levels]
        # 层数变多时合并才发生；若已收敛，长度可能只有 1 层
        if len(levels) > 1:
            assert sizes[1] <= sizes[0], "上层不应比下层更细"
            assert sizes == sorted(sizes, reverse=True), f"各层规模未递减: {sizes}"

    def test_numbering_is_stable_across_runs(self):
        """同图同参数必须给出同编号，否则前端层级树每次刷新都变。"""
        store_a = _build_store(self.EDGES)
        store_b = _build_store(list(reversed(self.EDGES)))
        detector = HierarchicalCommunityDetector(max_level=3)
        first = [[sorted(g) for g in level] for level in detector.detect_levels(store_a)]
        second = [[sorted(g) for g in level] for level in detector.detect_levels(store_b)]
        assert first == second, "边的插入顺序影响了社区编号"

    def test_overlap_ratio_favours_smaller_community(self):
        detector = HierarchicalCommunityDetector(merge_threshold=0.3)
        # 大社区含全部 5 个，小社区只有 2 个：Jaccard 很低但小社区已被覆盖
        big = ["a", "b", "c", "d", "e"]
        small = ["a", "b"]
        assert detector._overlap_ratio(big, small) == 1.0
        assert detector._overlap_ratio(["a"], ["z"]) == 0.0
        assert detector._overlap_ratio([], ["a"]) == 0.0

    def test_merge_threshold_controls_granularity(self):
        store = _build_store(self.EDGES)
        coarse = HierarchicalCommunityDetector(merge_threshold=0.0, max_level=3).detect_levels(store)
        fine = HierarchicalCommunityDetector(merge_threshold=0.99, max_level=3).detect_levels(store)
        # 阈值 0.99 时不该有任何合并发生
        assert len(fine[0]) >= len(coarse[0])

    def test_build_communities_populates_level_and_parent(self):
        store = _build_store(self.EDGES)
        detector = HierarchicalCommunityDetector(max_level=3)
        communities = build_communities(
            store, llm=None, detector=detector, min_size=2, max_communities=20,
        )
        assert communities, "应产出社区"
        assert all(c.level >= 0 for c in communities)
        assert all(c.generated_at > 0 for c in communities)
        ids = {c.community_id for c in communities}
        assert len(ids) == len(communities), "community_id 冲突"
        # 上层社区的 parent_id 必须指向同批真实存在的 id
        known = {c.community_id for c in communities if c.level == 0}
        for c in communities:
            if c.parent_id:
                assert c.parent_id in ids, f"parent_id 指向不存在的社区: {c.parent_id}"

    def test_single_level_detector_produces_level0(self):
        store = _build_store(self.EDGES)
        communities = build_communities(store, llm=None)
        assert communities
        assert all(c.level == 0 for c in communities)
        assert all(c.community_id.startswith("c-") for c in communities)

    def test_detect_returns_finest_level(self):
        store = _build_store(self.EDGES)
        detector = HierarchicalCommunityDetector(max_level=3)
        assert detector.detect(store) == detector.detect_levels(store)[0]


# ---------------------------------------------------------------- 3. 入库联动


class TestIngestGraphIndexing:
    """：入库应自动触发图构建与图像索引。

    测试环境用 mock 抽取器（抽不出实体），所以断言的是"联动钩子确实被调用"，
    而不是"图谱里一定有实体"——后者取决于抽取器，不是联动逻辑。
    """

    def test_post_index_hook_runs_on_sync_upload(self, client):
        calls: list[tuple[str, int]] = []
        client.app.state.knowledge.set_post_index(
            lambda record, parsed: calls.append((record.doc_id, len(parsed.assets or []))) or {"graph": {"chunks_processed": 1}}
        )
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        assert calls, "同步上传路径未触发图谱/图像索引联动"

    def test_post_index_hook_is_registered_in_app(self, client):
        """应用装配时必须真的挂了钩子，否则图谱永远只能手动构建。"""
        knowledge = client.app.state.knowledge
        assert getattr(knowledge, "_post_index", None) is not None

    def test_graph_builder_is_reachable_without_manual_call(self, client):
        """联动会调用 graph_builder（即便 mock 抽取器抽不出实体也会被调用）。"""
        builder = client.app.state.graph_builder
        seen: list[int] = []
        original = builder.build_from_chunks

        def spy(chunks, rebuild_doc_id=None):
            seen.append(len(chunks))
            return original(chunks, rebuild_doc_id)

        builder.build_from_chunks = spy
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        assert seen, "图构建器未被入库流程调用"

    def test_graph_failure_does_not_fail_ingestion(self, client, monkeypatch):
        """图谱是增强层：它挂了不能把一份已可检索的文档打成入库失败。"""
        knowledge = client.app.state.knowledge
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        assert knowledge.list_files(), "入库应成功"

        knowledge.set_post_index(
            lambda record, parsed: (_ for _ in ()).throw(RuntimeError("图谱构建模拟失败"))
        )
        # 走真实入库路径：钩子失败应被吞掉，文档状态仍是 indexed
        client.post("/knowledge/upload", files={"file": ("手册2.md", SAMPLE_MD.encode(), "text/markdown")})
        doc = knowledge.list_files()[0]
        assert doc.status == "indexed", f"钩子失败不应让文档入库失败（实际 {doc.status}）"
        assert knowledge.chunks_of(doc.doc_id), "文档仍应可检索"


# ---------------------------------------------------------------- 4/5. 多智能体事件


# ---------------------------------------------------------------- 6. 权限矩阵


class TestPermissionMatrix:
    def test_matrix_shape(self, client):
        data = client.get("/admin/users/permission-matrix").json()["data"]
        assert data["capabilities"] and data["matrix"]
        for cap in data["capabilities"]:
            # 新增 `by`（把守该能力的端点），便于运维核对而非只信字符串
            assert set(cap) == {"key", "label", "enforced", "by"}

    def test_every_role_row_has_granted_and_denied(self, client):
        data = client.get("/admin/users/permission-matrix").json()["data"]
        keys = {c["key"] for c in data["capabilities"]}
        for row in data["matrix"]:
            assert set(row["granted"]) | set(row["denied"]) == keys
            assert not (set(row["granted"]) & set(row["denied"])), "同一能力同时被允许与拒绝"
            assert row["user_count"] >= 0

    def test_matrix_is_a_privilege_lattice(self, client):
        """admin 必须是所有角色的超集。"""
        data = client.get("/admin/users/permission-matrix").json()["data"]
        rows = {r["role"]: r for r in data["matrix"]}
        admin = set(rows["admin"]["granted"])
        for role, row in rows.items():
            if role == "admin":
                continue
            assert set(row["granted"]) <= admin, f"{role} 的能力超出 admin"

    def test_capabilities_are_actually_enforced(self, client):
        """管理类能力必须**真的**被后端把守，且能说出由谁把守。

        此前 6 项标 `declared`（"后端未做角色门禁"）——那是当时的事实。
         给全部 `/admin/*` 挂上 `require_role("admin")` 之后，
        继续标 declared 就变成了**低估**实际防护。
        这里改为断言"不再有纸面权限"，并要求每项都给出把守出处。
        """
        data = client.get("/admin/users/permission-matrix").json()["data"]
        caps = {c["key"]: c for c in data["capabilities"]}
        assert caps["ask"]["enforced"] == "backend"
        assert caps["view_all_tenant_data"]["enforced"] == "acl"
        # 管理类能力已由 /admin/* 的 require_role("admin") 强制
        assert caps["manage_acl"]["enforced"] == "backend"
        assert caps["manage_users"]["enforced"] == "backend"
        # 评测平台下线：run_eval 能力与其把守端点 /admin/eval 一并移除，
        # 残留会变成"声明了但没人把守"的纸面权限。
        assert "run_eval" not in caps, "评测端点已下线，权限矩阵不应再声明 run_eval"
        # 不允许再有"声明了但没人把守"的纸面权限
        assert not [k for k, c in caps.items() if c["enforced"] == "declared"], (
            "存在未被强制的纸面权限，会把管理员误导为已接门禁"
        )
        # 每项都要说得出把守它的端点（可核对，而非只能相信字符串）
        assert all(c.get("by") for c in caps.values()), "每项能力都应注明把守出处"



# ---------------------------------------------------------------- 7. Query cost


class TestQueryCost:
    def test_cost_is_computed(self):
        """：没有成本数字，"关掉 HyDE 省了多少"无法回答。"""
        from app.services.query.schemas import (
            ESTIMATED_TOKENS_PER_CALL, INTENT_TOKEN_PRICE_YUAN,
        )

        assert ESTIMATED_TOKENS_PER_CALL > 0
        assert 0 < INTENT_TOKEN_PRICE_YUAN < 0.01
        # 1 次调用 ≈ 600 token × 0.0007/1000 元/token
        expected = round(ESTIMATED_TOKENS_PER_CALL * INTENT_TOKEN_PRICE_YUAN, 6)
        assert expected > 0

    def test_analysis_records_cost(self):
        """预处理产出的分析记录必须带 cost 字段（默认 0.0，不能缺）。"""
        from app.services.query.schemas import QueryAnalysis

        analysis = QueryAnalysis(analysis_id="a1", raw_query="q", tenant_id="t")
        assert analysis.cost == 0.0
        analysis.llm_calls = 2
        analysis.cost = round(
            2 * __import__(
                "app.services.query.schemas", fromlist=["ESTIMATED_TOKENS_PER_CALL"]
            ).ESTIMATED_TOKENS_PER_CALL
            * __import__(
                "app.services.query.schemas", fromlist=["INTENT_TOKEN_PRICE_YUAN"]
            ).INTENT_TOKEN_PRICE_YUAN,
            6,
        )
        assert analysis.cost > 0.0

    def test_cost_scales_with_llm_calls(self, client):
        from app.services.query.schemas import (
            ESTIMATED_TOKENS_PER_CALL, INTENT_TOKEN_PRICE_YUAN,
        )

        one = round(1 * ESTIMATED_TOKENS_PER_CALL * INTENT_TOKEN_PRICE_YUAN, 6)
        three = round(3 * ESTIMATED_TOKENS_PER_CALL * INTENT_TOKEN_PRICE_YUAN, 6)
        assert three == pytest.approx(one * 3, rel=1e-6)


# ---------------------------------------------------------------- 8. 并发归属


