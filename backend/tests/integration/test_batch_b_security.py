"""批次 B（P0 安全与正确性）改造验证测试。

每个用例对应文档 §5 的缺陷编号，验证修复真实生效而非形同虚设：
- 鉴权矩阵：管理端点从路由表自动枚举，非 admin 一律 403
- 缓存隔离（ 跨租户 /  租户内身份+ACL 版本）
- SQL 租户隔离
- 索引版本过滤（ / ）
- Agent 审计解耦
"""
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

ADMIN_HEADERS = {"X-User-Id": "admin", "X-Tenant-Id": "default", "X-Roles": "admin"}
_NON_ADMIN = {"X-User-Id": "alice", "X-Roles": ""}


@contextmanager
def make_client(**env_overrides):
    """按需构造指定环境配置的 client（用于切换 acl_enabled 等装配期配置）。"""
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "DATA_DIR": tmp, "MODEL_PROVIDER": "mock", "AUTH_ENABLED": "false",
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
            with TestClient(create_app(), headers=ADMIN_HEADERS) as c:
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


class TestAuthMatrix:
    """管理端点鉴权矩阵（从路由表自动枚举，禁止硬编码清单）。"""

    def test_admin_route_count_is_stable(self, client):
        routes = _admin_routes(client.app)
        # 数量守卫：端点增删必须显式更新本断言，避免悄悄新增裸奔端点。
        #  新增 GET /admin/costs； 新增 GET /admin/tools → 30。
        # 评测平台下线：移除 /admin/eval/{datasets,runs,runs/{id},run,compare} 5 个
        # + /admin/metrics/multiagent 1 个 → 24；/admin/costs 由 reports.py 迁入
        # admin.py，路径与门禁等价，总数不变 → 23。
        # 账号密码档：新增 PUT /admin/users/{user_id}/password（管理员为账号设密）
        # → 24。注意 /auth/login、/auth/register 不在本守卫范围内——它们按设计
        # 必须是公开端点，把它们划进 /admin/ 才是错误。
        # 知识库管理：新增 /admin/kb（GET/POST）+ /admin/kb/{id}（PUT/DELETE）
        # + grants（GET/PUT）+ documents（GET/PUT）+ /admin/users/{id}/kbs（GET）→ 33。
        # 敏感词/配额/审计下线：移除 audit×2 + quota×3 + sensitive×3 = 8 个 → 25。
        assert len(routes) == 25, f"管理端点数量变化（当前 {len(routes)}），需显式确认并更新"

    def test_every_admin_route_rejects_non_admin(self, client):
        routes = _admin_routes(client.app)
        assert routes, "未枚举到任何管理端点，鉴权矩阵失去意义"
        for method, path in routes:
            url = _fill(path)
            resp = client.request(method, url, headers=_NON_ADMIN)
            assert resp.status_code == 403, (
                f"{method} {url} 非 admin 应 403，实得 {resp.status_code}"
            )

    def test_every_admin_route_accepts_admin(self, client):
        for method, path in _admin_routes(client.app):
            url = _fill(path)
            resp = client.request(method, url)  # client 默认注入 admin 身份
            assert resp.status_code not in (401, 403), (
                f"{method} {url} admin 不应被 {resp.status_code} 拦截"
            )


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

    def test_cache_key_invalidated_by_acl_change(self, client):
        pipeline = client.app.state.pipeline.bind_principal("t1", "alice")
        acl = client.app.state.acl_service
        config = client.app.state.config_store.get_active()
        k_before = pipeline._cache_key("同问", "t1", config)
        acl.set_acl("doc-x", [], [], True, updated_by="admin")
        k_after = pipeline._cache_key("同问", "t1", config)
        assert k_before != k_after, "ACL 变更后旧缓存键必须失效（否则权限撤销后仍命中）"


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

    def test_graph_api_scoped_by_tenant(self, client):
        """端到端：跨租户请求看不到对方实体。"""
        from app.domain.schemas import EntityNode
        store = client.app.state.graph_store
        store.upsert_entity(EntityNode(entity_id="x", name="租户机密", tenant_id="tenant-a"))

        other = client.get("/graph/entities", headers={"X-Tenant-Id": "tenant-b"})
        assert other.status_code == 200
        assert all(e["name"] != "租户机密" for e in other.json()["data"]), "跨租户泄漏实体"

        owner = client.get("/graph/entities", headers={"X-Tenant-Id": "tenant-a"})
        assert any(e["name"] == "租户机密" for e in owner.json()["data"])

    def test_graph_stats_and_detail_scoped(self, client):
        """统计与实体详情同样限定租户。"""
        from app.domain.schemas import EntityNode
        store = client.app.state.graph_store
        store.upsert_entity(EntityNode(entity_id="y", name="密实体", tenant_id="tenant-a"))

        detail = client.get("/graph/entity/密实体", headers={"X-Tenant-Id": "tenant-b"})
        assert detail.status_code == 404, "跨租户查实体应 404"

        stats_a = client.get("/graph/stats", headers={"X-Tenant-Id": "tenant-a"}).json()["data"]
        stats_b = client.get("/graph/stats", headers={"X-Tenant-Id": "tenant-b"}).json()["data"]
        assert stats_a["entities"] > stats_b["entities"]


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
            headers={"X-Tenant-Id": "default", "X-User-Id": "admin", "X-Roles": "admin"},
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
            headers={"X-Tenant-Id": "default", "X-User-Id": "admin", "X-Roles": "admin"},
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
            headers={"X-Tenant-Id": "default", "X-User-Id": "admin", "X-Roles": "admin"},
        )
        assert r.status_code == 404, f"跨主体 run_id 应 404，实得 {r.status_code}"

    def test_interrupt_status_rejects_foreign_run_id(self, client):
        r = client.get(
            "/ask/runs/other-tenant:other-user:s1:1/interrupt",
            headers={"X-Tenant-Id": "default", "X-User-Id": "admin", "X-Roles": "admin"},
        )
        assert r.status_code == 404

    def test_interrupt_status_for_unstarted_run_is_not_awaiting(self, client):
        """未挂起的 run 返回 awaiting=False（而不是报错），前端可安全轮询。"""
        r = client.get(
            "/ask/runs/default:admin:nonexistent:1/interrupt",
            headers={"X-Tenant-Id": "default", "X-User-Id": "admin", "X-Roles": "admin"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["awaiting"] is False

    def test_hitl_disabled_by_default(self, client):
        """默认关闭：HITL 对用户是打断，需前端配合才能开，不能后端单方面打开。"""
        assert client.app.state.settings.agent_hitl_enabled is False

    def test_resume_requires_auth(self):
        """未认证访问 resume 必须 401。

        这里刻意**不用** `client` fixture——它默认注入 admin 头（为了让绝大多数
        用例免于写认证样板），无法表达"匿名请求"。因此单独构造一个带头部的 client。
        """
        import os
        import tempfile

        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "DATA_DIR": tmp, "MODEL_PROVIDER": "mock", "AUTH_ENABLED": "true",
                "AUTH_STATIC_USERS": "admin:admin", "VECTOR_STORE": "faiss",
                "RETRIEVAL_CONFIG_APPLY_DELAY_S": "0",
                # auth_enabled=true 必须有凭证来源，否则启动期校验会拒绝启动
                "AUTH_TOKENS_JSON": '{"tok-admin": {"user_id": "admin", "tenant_id": "default", "roles": ["admin"]}}',
            }
            saved = {k: os.environ.get(k) for k in env}
            os.environ.update(env)
            try:
                from app.core.config import get_settings

                get_settings.cache_clear()
                from app.main import create_app

                with TestClient(create_app()) as anon:  # 不带任何头部
                    r = anon.post(
                        "/ask/runs/default:admin:s1:1/resume", json={"action": "stop"}
                    )
                    assert r.status_code == 401, f"匿名 resume 应 401，实得 {r.status_code}"
            finally:
                get_settings.cache_clear()
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
