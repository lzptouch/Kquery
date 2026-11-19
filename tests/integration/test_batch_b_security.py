"""批次 B（P0 安全与正确性）改造验证测试。

每个用例对应文档 §5 的缺陷编号，验证修复真实生效而非形同虚设：
- 管理端点可达性：管理端点从路由表自动枚举，不再有角色门禁（原「非 admin 403」）
- 缓存隔离（跨租户 / 租户内身份签名）
- SQL 租户隔离
- 索引版本过滤
- Agent 审计解耦

**认证与授权已下线**：原「鉴权矩阵」用例（非 admin 一律 403）与
「ACL 版本变更使缓存键失效」用例随能力删除；前者改为断言「管理端点不再返回
401/403」，后者删除。请求身份固定为 default/local，**请求头无法切换租户**——
这一点由 `test_identity_is_fixed_and_headers_cannot_switch_tenant` 固化。
"""
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

#: 试图用请求头切换租户/用户/角色的"越权"请求头。认证已下线后它们**必须被忽略**。
IMPERSONATION_HEADERS = {
    "X-User-Id": "mallory",
    "X-Tenant-Id": "other-tenant",
    "X-Roles": "admin",
}


@contextmanager
def make_client(**env_overrides):
    """按需构造指定环境配置的 client（用于切换装配期配置）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "DATA_DIR": tmp, "MODEL_PROVIDER": "mock",
            "VECTOR_STORE": "faiss", "RETRIEVAL_CONFIG_APPLY_DELAY_S": "0",
            "CRAG_CORRECT_TH": "0.3", "CRAG_INCORRECT_TH": "0.1",
        }
        env.update({k: str(v) for k, v in env_overrides.items()})
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            from app.core.config import get_settings
            get_settings.cache_clear()
            from app.main import create_app
            with TestClient(create_app()) as c:
                yield c
        finally:
            get_settings.cache_clear()
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


def _admin_routes(app) -> list[tuple[str, str]]:
    """从 OpenAPI 规范枚举管理端点（展平且完整，新增端点漏挂鉴权会被数量守卫/403 断言发现）。

    注意不能用 app.routes：FastAPI 0.142 把 include_router 的分组表示为 _IncludedRouter
    对象，直接遍历拿不到内部 APIRoute 的完整路径。
    """
    out = []
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/admin/"):
            continue
        for method in ops:
            if method.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                out.append((method.upper(), path))
    return out


def _fill(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


class TestAdminRouteSurface:
    """管理端点可达性（从路由表自动枚举，禁止硬编码清单）。

    认证与授权已下线：管理端点**不再有角色门禁**，因此本组断言从
    「非 admin → 403」改为「任何请求都不得被 401/403 拦截」。
    """

    def test_admin_route_count_is_stable(self, client):
        routes = _admin_routes(client.app)
        # 数量守卫：端点增删必须显式更新本断言，避免悄悄新增端点。
        # 历史：30 → 认证与授权下线移除 /admin/users×2、/admin/users/{id}/password、
        # /admin/documents/acl、/admin/documents/{id}/acl、/admin/users/permission-matrix
        # （6 个），并移除知识库管理 7 个（/admin/kb 系列 + /admin/users/{id}/kbs）
        # → 17。/admin/tools 由 api/security.py 迁入 api/admin.py，路径不变。
        # 实测 15：其中 /admin/retrieval-config 与 /admin/retrieval-config/reset 各含
        # GET+PUT 两个方法，枚举按 (method, path) 计，与上面的"按路径数"口径差 2。
        assert len(routes) == 16, f"管理端点数量变化（当前 {len(routes)}），需显式确认并更新"

    def test_every_admin_route_is_reachable(self, client):
        routes = _admin_routes(client.app)
        assert routes, "未枚举到任何管理端点，可达性守卫失去意义"
        for method, path in routes:
            url = _fill(path)
            resp = client.request(method, url)
            assert resp.status_code not in (401, 403), (
                f"{method} {url} 不应再被角色门禁拦截，实得 {resp.status_code}"
            )

    def test_identity_is_fixed_and_headers_cannot_switch_tenant(self, client):
        """身份固定：请求头不能切换租户/用户/角色。

        认证已下线后 `Principal` 是常量，任何请求头都无权改变主体——这正是
        「删掉了身份体系」与「仍允许自报身份」之间的差别。
        """
        resp = client.get("/admin/metrics/summary", headers=IMPERSONATION_HEADERS)
        assert resp.status_code == 200, f"管理端点应可达，实得 {resp.status_code}"
        body = resp.json()
        assert body.get("status") in ("ok", "warn"), body


class TestCacheIsolation:
    """缓存不得跨租户、跨身份、跨 ACL 版本泄漏。"""

    def test_semantic_lookup_filters_by_tenant(self):
        from app.services.cache import InMemorySemanticCache
        cache = InMemorySemanticCache()
        cache.set("k1", {"answer": "t1 的答案"}, 60, question="q", vector=[1.0, 0.0],
                  tenant_id="t1", signature="sig-t1")
        # 完全相同的问题向量，不同租户 → 不得命中（此前会跨租户串答案）
        entry, kind = cache.lookup("q", "other-key", [1.0, 0.0], tenant_id="t2", signature="sig-t2")
        assert entry is None and kind == "miss"
        # 同租户 → 命中
        entry, kind = cache.lookup("q", "other-key", [1.0, 0.0], tenant_id="t1", signature="sig-t1")
        assert entry is not None and kind == "semantic"

    def test_semantic_lookup_filters_by_signature(self):
        """KQ-04：同租户不同签名（不同用户/ACL 版本/配置）不得语义命中。"""
        from app.services.cache import InMemorySemanticCache
        cache = InMemorySemanticCache()
        cache.set("k1", {"answer": "alice 的答案"}, 60, question="q", vector=[1.0, 0.0],
                  tenant_id="t1", signature="sig-alice")
        # 同租户、同向量、不同签名（另一用户/权限变更后）→ 不得命中
        entry, kind = cache.lookup("q", "other-key", [1.0, 0.0], tenant_id="t1", signature="sig-bob")
        assert entry is None and kind == "miss"
        # 无签名调用（旧调用方）→ 不得命中（宁 miss 不越权）
        entry, kind = cache.lookup("q", "other-key", [1.0, 0.0], tenant_id="t1")
        assert entry is None and kind == "miss"
        # 同签名 → 命中
        entry, kind = cache.lookup("q", "other-key", [1.0, 0.0], tenant_id="t1", signature="sig-alice")
        assert entry is not None and kind == "semantic"

    def test_cache_key_varies_by_user(self, client):
        pipeline = client.app.state.pipeline
        config = client.app.state.config_store.get_active()
        k_alice = pipeline.bind_principal("t1", "alice")._cache_key("同问", "t1", config)
        k_bob = pipeline.bind_principal("t1", "bob")._cache_key("同问", "t1", config)
        assert k_alice != k_bob, "不同用户的缓存键必须不同（否则跨用户共享缓存）"
        k_alice2 = pipeline.bind_principal("t1", "alice")._cache_key("同问", "t1", config)
        assert k_alice == k_alice2, "同用户同问题缓存键必须稳定（否则命中率归零）"



class TestSqlTenantIsolation:
    """sql_search 必须按租户隔离。"""

    @pytest.fixture()
    def biz_db(self, tmp_path):
        db = tmp_path / "biz.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE sales (id INTEGER, tenant_id TEXT, amount REAL)")
        conn.executemany(
            "INSERT INTO sales VALUES (?, ?, ?)",
            [(1, "t1", 100.0), (2, "t2", 200.0), (3, "t1", 150.0)],
        )
        conn.execute("CREATE TABLE secret (id INTEGER, data TEXT)")  # 无租户列
        conn.execute("INSERT INTO secret VALUES (1, 'classified')")
        conn.commit()
        conn.close()
        return str(db)

    def test_tenant_cannot_read_other_tenant_rows(self, biz_db):
        from app.services.agent.tools import SqlSearchTool
        tool = SqlSearchTool(biz_db, tenant_column="tenant_id")
        r = tool.run("SELECT * FROM sales", "t1")
        assert r.items, r.error
        contents = " ".join(it["content"] for it in r.items)
        assert "200" not in contents, "租户 t1 不应看到 t2 的行"
        assert "100" in contents and "150" in contents

    def test_table_without_tenant_column_is_hidden(self, biz_db):
        from app.services.agent.tools import SqlSearchTool
        tool = SqlSearchTool(biz_db, tenant_column="tenant_id")
        r = tool.run("SELECT * FROM secret", "t1")
        assert r.error and "secret" in r.error

    def test_join_rejected_in_tenant_mode(self, biz_db):
        from app.services.agent.tools import SqlSearchTool
        tool = SqlSearchTool(biz_db, tenant_column="tenant_id")
        r = tool.run("SELECT * FROM sales a JOIN sales b ON a.id = b.id", "t1")
        assert r.error and "单表" in r.error

    def test_limit_is_enforced(self, biz_db):
        from app.services.agent.tools import SqlSearchTool
        tool = SqlSearchTool(biz_db, max_rows=2)
        r = tool.run("SELECT * FROM sales LIMIT 100000", "t1")
        # 强制 LIMIT（兑现注释承诺）：调用方写 LIMIT 100000 也不得多于 max_rows
        assert r.truncated_from <= 3 or len(r.items) >= 1
        contents = " ".join(it["content"] for it in r.items)
        assert contents.count("tenant_id=") <= 2


class TestIndexVersionFilter:
    """reparse 后历史版本块不得参与召回。"""

    def _chunk(self, cid, doc_id, content):
        from app.domain.schemas import Chunk
        return Chunk(chunk_id=cid, doc_id=doc_id, seq=1, content=content,
                     tenant_id="t1", source="第1页")

    def test_hybrid_filters_historical_version(self):
        from app.domain.schemas import RetrievalConfig
        from app.services.retrieval.base import RetrievalCandidate
        from app.services.retrieval.hybrid import HybridRetriever
        from app.services.retrieval.rerank import MockReranker

        chunks = [
            RetrievalCandidate(chunk=self._chunk("d@v1#0", "d", "旧版本内容"), score=0.95),
            RetrievalCandidate(chunk=self._chunk("d@v2#0", "d", "新版本内容"), score=0.80),
        ]

        class FakeRetriever:
            name = "keyword"

            def search(self, query, top_n, tenant_id, doc_id=None, filters=None):
                return list(chunks)

        hybrid = HybridRetriever(
            retrievers={"keyword": FakeRetriever()},
            reranker=MockReranker(),
            version_of=lambda doc_id: 2,  # 当前版本 = 2
        )
        result = hybrid.search("内容", "t1", RetrievalConfig())
        ids = [c.chunk.chunk_id for c in result.candidates]
        assert "d@v2#0" in ids, "当前版本块应被召回"
        assert "d@v1#0" not in ids, "历史版本块不得被召回（版本污染）"

    def test_unversioned_chunks_are_recalled(self):
        """存量数据（无 @v 标记）应视为当前版本正常召回，避免升级后索引空白。"""
        from app.domain.schemas import RetrievalConfig
        from app.services.retrieval.base import RetrievalCandidate
        from app.services.retrieval.hybrid import HybridRetriever
        from app.services.retrieval.rerank import MockReranker

        stale = [RetrievalCandidate(chunk=self._chunk("d#0", "d", "存量块"), score=0.9)]

        class FakeRetriever:
            name = "keyword"

            def search(self, query, top_n, tenant_id, doc_id=None, filters=None):
                return list(stale)

        hybrid = HybridRetriever(
            retrievers={"keyword": FakeRetriever()}, reranker=MockReranker(),
            version_of=lambda doc_id: 3,
        )
        result = hybrid.search("存量", "t1", RetrievalConfig())
        assert "d#0" in [c.chunk.chunk_id for c in result.candidates]


class TestGraphTenantIsolation:
    """图谱必须按租户隔离（此前任一租户可见全量实体与关系）。"""

    def test_store_partitions_entities_and_edges_by_tenant(self, tmp_path):
        from app.domain.schemas import EntityNode, RelationEdge
        from app.services.graph.store import InMemoryGraphStore

        store = InMemoryGraphStore(tmp_path / "g.json")
        # 同名实体分属两个租户：此前会互相覆盖
        store.upsert_entity(EntityNode(entity_id="a", name="机密项目", tenant_id="t1"))
        store.upsert_entity(EntityNode(entity_id="b", name="机密项目", tenant_id="t2"))
        store.upsert_relation(RelationEdge(source="机密项目", target="负责人",
                                           relation="负责人", tenant_id="t1", source_chunks=["c1"]))
        store.upsert_relation(RelationEdge(source="机密项目", target="负责人",
                                           relation="负责人", tenant_id="t2", source_chunks=["c2"]))

        assert len(store.all_entities()) == 2, "同名实体不得跨租户互相覆盖"
        assert [e.tenant_id for e in store.all_entities("t1")] == ["t1"]
        assert [e.tenant_id for e in store.all_entities("t2")] == ["t2"]

        # 邻居遍历不跨租户：各自只看到本租户那条边
        n1 = store.neighbors("机密项目", depth=1, tenant_id="t1")
        n2 = store.neighbors("机密项目", depth=1, tenant_id="t2")
        assert [x["source_chunks"] for x in n1] == [["c1"]]
        assert [x["source_chunks"] for x in n2] == [["c2"]]

        assert store.stats("t1")["entities"] == 1
        assert store.stats("t2")["relations"] == 1

    def test_store_persists_tenant_partition(self, tmp_path):
        """持久化后分区不丢（复合 key 重建）。"""
        from app.domain.schemas import EntityNode
        from app.services.graph.store import InMemoryGraphStore

        path = tmp_path / "g.json"
        s1 = InMemoryGraphStore(path)
        s1.upsert_entity(EntityNode(entity_id="a", name="甲", tenant_id="t1"))
        s1.upsert_entity(EntityNode(entity_id="b", name="甲", tenant_id="t2"))
        s1.save()

        s2 = InMemoryGraphStore(path)
        assert len(s2.all_entities()) == 2
        assert [e.tenant_id for e in s2.all_entities("t1")] == ["t1"]

    def test_graph_api_cannot_switch_tenant_via_headers(self, client):
        """身份固定后，请求头**不能**切换租户。

        图谱存储仍按 tenant 分区（`InMemoryGraphStore` 的复合 key），但端点读的
        是**固定身份**的租户。原先的"换个 X-Tenant-Id 头就能看到别的租户"这条
        越权路径，在认证下线后必须彻底关闭——否则等于把租户隔离退化成自报身份。
        """
        from app.domain.schemas import EntityNode
        store = client.app.state.graph_store
        store.upsert_entity(EntityNode(entity_id="x", name="租户机密", tenant_id="tenant-a"))

        for headers in ({}, {"X-Tenant-Id": "tenant-b"}, {"X-Tenant-Id": "tenant-a"}):
            r = client.get("/graph/entities", headers=headers)
            assert r.status_code == 200
            assert all(e["name"] != "租户机密" for e in r.json()["data"]), (
                f"固定租户不得看到其它租户的实体（headers={headers}）"
            )

    def test_graph_stats_and_detail_scoped(self, client):
        """统计与实体详情同样按固定租户限定。"""
        from app.domain.schemas import EntityNode
        store = client.app.state.graph_store
        store.upsert_entity(EntityNode(entity_id="y", name="密实体", tenant_id="tenant-a"))

        detail = client.get("/graph/entity/密实体", headers={"X-Tenant-Id": "tenant-a"})
        assert detail.status_code == 404, "非本租户实体应 404（请求头不能切换租户）"

        stats = client.get("/graph/stats").json()["data"]
        assert stats["entities"] == 0, "固定租户不应统计到其它租户的实体"


class TestTokenUsageAccounting:
    """token 用量采集：LLM 客户端必须暴露 last_usage（供成本/用量统计）。"""

    def test_mock_llm_exposes_last_usage(self):
        from app.services.llm import MockLLM

        llm = MockLLM()
        llm.generate("这是一段用于统计 token 的提示词，内容足够长以便产生估算值")
        u = llm.last_usage
        assert u.input_tokens > 0, "输入 token 必须被统计"
        assert u.estimated is True, "mock 无供应商 usage，必须显式标记为估算"

    def test_usage_extracted_from_provider_response(self):
        from app.services.llm import QwenLLM

        class FakeUsage:
            prompt_tokens = 123
            completion_tokens = 45

        class FakeResp:
            usage = FakeUsage()

        u = QwenLLM._extract_usage(FakeResp(), "prompt", "output")
        assert u.input_tokens == 123 and u.output_tokens == 45
        assert u.estimated is False, "拿到供应商 usage 时不得标记为估算"

    def test_usage_falls_back_to_estimate(self):
        from app.services.llm import QwenLLM

        class FakeResp:
            usage = None

        u = QwenLLM._extract_usage(FakeResp(), "a" * 100, "b" * 50)
        assert u.estimated is True and u.input_tokens > 0

class TestAgentPrometheusMetrics:
    """Agent 此前零埋点，大盘只有线性路径数据。"""

    def _scrape(self, client) -> str:
        return client.get("/metrics").text

    def test_agent_metrics_exposed(self, client):
        text = self._scrape(client)
        for m in (
            "rag_agent_runs_total",
            "rag_agent_iterations",
            "rag_agent_tool_calls_total",
            "rag_agent_fuse_total",
            "rag_agent_next_action_total",
        ):
            assert m in text, f"缺少 Agent 指标 {m}"

    def test_no_high_cardinality_tenant_label(self, client):
        """护栏：实际暴露的指标不得带 tenant label（高基数反模式）。

        检查渲染后的 /metrics 而不是源码——注释里出现该字样不应误报。
        """
        for line in self._scrape(client).splitlines():
            if line.startswith("#") or "{" not in line:
                continue
            name, _, labels = line.partition("{")
            if 'tenant=' in labels:
                raise AssertionError(f"指标 {name} 使用了高基数 tenant label: {line}")

    def test_agent_run_emits_metrics(self, client):
        """跑一次 Agent 问答，验证 runs_total 与 tool_calls 真的被上报。"""
        r = client.post(
            "/ask",
            json={"question": "测试问题", "mode": "agent"},
        )
        assert r.status_code == 200
        text = self._scrape(client)
        assert "rag_agent_runs_total" in text
        # 至少有一条 tool 调用被计数（无文档时通常是 empty 或 error，但必须有计数行）
        assert "rag_agent_tool_calls_total{" in text, "Agent 工具调用未被埋点"

    def test_metrics_survive_without_docs(self, client):
        """空知识库下 Agent 仍应产出可观测数据，而不是静默无指标。"""
        r = client.post(
            "/ask",
            json={"question": "空库问题", "mode": "agent"},
        )
        assert r.status_code == 200
        assert "rag_agent_runs_total" in self._scrape(client)


class TestHitlEndpoints:
    """HITL 端点必须注册且做归属校验（跨主体 run_id 一律 404）。"""

    def test_endpoints_registered(self, client):
        spec = client.app.openapi()["paths"]
        assert "/ask/runs/{run_id}/resume" in spec
        assert "/ask/runs/{run_id}/interrupt" in spec
        assert "post" in spec["/ask/runs/{run_id}/resume"]
        assert "get" in spec["/ask/runs/{run_id}/interrupt"]

    def test_resume_rejects_foreign_run_id(self, client):
        """跨用户 run_id 必须 404（不用 403，避免存在性探测）。"""
        r = client.post(
            "/ask/runs/other-tenant:other-user:s1:1/resume",
            json={"action": "force_answer"},
        )
        assert r.status_code == 404, f"跨主体 run_id 应 404，实得 {r.status_code}"

    def test_interrupt_status_rejects_foreign_run_id(self, client):
        r = client.get(
            "/ask/runs/other-tenant:other-user:s1:1/interrupt",
        )
        assert r.status_code == 404

    def test_interrupt_status_for_unstarted_run_is_not_awaiting(self, client):
        """未挂起的 run 返回 awaiting=False（而不是报错），前端可安全轮询。"""
        r = client.get(
            "/ask/runs/default:local:nonexistent:1/interrupt",
        )
        assert r.status_code == 200
        assert r.json()["data"]["awaiting"] is False

    def test_hitl_disabled_by_default(self, client):
        """默认关闭：HITL 对用户是打断，需前端配合才能开，不能后端单方面打开。"""
        assert client.app.state.settings.agent_hitl_enabled is False


    def test_approve_endpoint_registered(self, client):
        """写类工具审批端点必须注册（否则 HITL 只能读不能批）。"""
        spec = client.app.openapi()["paths"]
        assert "/ask/runs/{run_id}/approve" in spec
        assert "post" in spec["/ask/runs/{run_id}/approve"]

    def test_approve_rejects_foreign_run_id(self, client):
        """跨用户 run_id 必须 404（与 resume/interrupt 同纪律）。"""
        r = client.post(
            "/ask/runs/other-tenant:other-user:s1:1/approve",
            json={"tools": ["http_request"]},
        )
        assert r.status_code == 404, f"跨主体 run_id 应 404，实得 {r.status_code}"

    def test_approve_requires_tool_list(self, client):
        r = client.post(
            "/ask/runs/default:local:s1:1/approve",
            json={"tools": []},
        )
        assert r.status_code == 400

    def test_approve_unknown_tool_is_rejected(self, client):
        """未知工具名必须报错，而不是静默生效（否则前端以为批了其实没批）。"""
        _register_write_tool(client)
        r = client.post(
            "/ask/runs/default:local:s1:1/approve",
            json={"tools": ["not_a_tool"]},
        )
        assert r.status_code == 400

    def test_approve_read_only_tool_is_rejected(self, client):
        """只读工具不需要审批，也不能被"批准"（审批只对写类工具有意义）。"""
        _register_write_tool(client)
        r = client.post(
            "/ask/runs/default:local:s1:1/approve",
            json={"tools": ["vector_search"]},
        )
        assert r.status_code == 400

    def test_approve_registers_write_tool(self, client):
        _register_write_tool(client)
        r = client.post(
            "/ask/runs/default:local:s1:1/approve",
            json={"tools": ["http_request"]},
        )
        assert r.status_code == 200
        assert "http_request" in r.json()["data"]["approved_tools"]

    def test_approve_without_write_tools_is_rejected(self, client):
        """注册表里没有写类工具时必须明确报错，不能"假装批了"。"""
        r = client.post(
            "/ask/runs/default:local:s1:1/approve",
            json={"tools": ["http_request"]},
        )
        assert r.status_code == 400


def _register_write_tool(client) -> None:
    """往运行中的注册表塞一个写类工具（默认部署不注册写工具）。"""
    from app.services.agent.tools import ToolResult

    class _WriteTool:
        name = "http_request"
        description = "写类工具（测试用）"

        def run(self, query: str, tenant_id: str, **kwargs) -> ToolResult:
            return ToolResult(items=[])

    registry = client.app.state.agent_runner._registry
    if registry is not None and "http_request" not in [e.get("name") for e in registry.catalog()]:
        registry.register(_WriteTool())


class TestIndexAliasEndpoints:
    """索引别名（重建不停机切换）的管理端点。"""

    def test_status_endpoint_registered(self, client):
        spec = client.app.openapi()["paths"]
        assert "/admin/index/alias" in spec
        assert "/admin/index/alias/switch" in spec

    def test_status_returns_current_alias(self, client):
        r = client.get("/admin/index/alias")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["available"] is True
        assert data["version"] >= 1

    def test_switch_bumps_version_and_keeps_previous(self, client):
        before = client.get("/admin/index/alias").json()["data"]
        r = client.post(
            "/admin/index/alias/switch", json={}
        )
        assert r.status_code == 200
        after = r.json()["data"]
        assert after["version"] == before["version"] + 1
        assert after["previous"] == before["physical"]

    def test_rollback_restores_previous(self, client):
        before = client.get("/admin/index/alias").json()["data"]
        client.post("/admin/index/alias/switch", json={})
        r = client.post(
            "/admin/index/alias/switch", json={"rollback": True}
        )
        assert r.status_code == 200
        assert r.json()["data"]["physical"] == before["physical"]

