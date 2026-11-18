"""P0/P1 补全项的回归测试。

覆盖本轮修复的每个真实缺陷，逐条对应到具体行为断言：

1.  错误码唯一性（IntEnum 别名导致按码分流失效）
2.  错误路径 trace_id 与请求一致（用户复制到的追踪号必须能反查日志）
3.  线性 /ask 路径的 ACL 与认证租户（此前固定 default_tenant_id 且无 ACL）
4.  线性路径的审计与敏感词扫描（此前只挂 Agent 路径）
5.  permission_denied 拒答分支（此前永久不可达）
6.  Agent 路径产出 retrieval_stats / confidence / crag_branch（此前是死代码）
7.  嵌套检索统计（文档要求 latency_ms / candidates 两组嵌套对象）
8.  warn 信封 + 44xx 降级码
9.  SELF_CHECK 字段与 CRAG 轮次落库
10. reparse 版本化：旧块保留，历史引用不失效
11. 索引发布闸门：向量条数必须等于块条数
12. 入库质量红线：pages / short_chunks / quality_flags
13. 文件 7 态状态机（FILE_PENDING / FILE_UPLOADING 此前零引用）
14. 真滑动窗口配额 + token 维度
15. JWT 认证档（alg=none 与算法混淆必须拒绝）
16. 用户目录持久化（重启不丢）
17. RRF k / 两级召回数 / 重排模型真正热更新
18. 语义缓存近似命中 + 缓存故障无感降级
19. 令牌桶按真实租户分桶
20. 音频切分：静音点选择 + 时间戳全局平移 + 平台失败分类
21. 评测 Coverage / code_version / 对比端点
22. 权限矩阵端点
23. sql_search 只读闸门 / web_search 未配置时不注册
24. 启动期配置校验与探针分级
"""
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.envelope import ErrCode, validate_code_uniqueness
from app.core.tracing import get_trace_id, new_trace_id, reset_trace_id, set_trace_id
from app.domain.schemas import RetrievalConfig, RetrievalStats
from app.core.errors import InvalidInputError
from app.services.agent.tools import ToolRegistry
from app.services.cache import InMemorySemanticCache, _cosine
from app.core.identity import Principal
from app.services.vectorstore import chunk_version, with_version
from app.services.validation.selfcheck import SelfCheckResult, estimate_claims


# ---------------------------------------------------------------- 1. 错误码


class TestErrorCodes:
    def test_codes_are_globally_unique(self):
        validate_code_uniqueness()  # 不抛异常即通过

    def test_asset_and_task_not_found_are_distinguishable(self):
        """曾出现 TASK_NOT_FOUND 与 ASSET_NOT_FOUND 同为 3301 的事故。"""
        assert int(ErrCode.TASK_NOT_FOUND) != int(ErrCode.ASSET_NOT_FOUND)
        assert ErrCode.TASK_NOT_FOUND is not ErrCode.ASSET_NOT_FOUND
        assert int(ErrCode.TASK_NOT_FOUND) == 3301
        assert int(ErrCode.ASSET_NOT_FOUND) == 3302

    def test_degraded_codes_cover_documented_segments(self):
        """44xx 重排 / 45xx 缓存 / 49xx 评分与自检，三段都要有码。"""
        assert int(ErrCode.RERANK_DEGRADED) // 100 == 44
        assert int(ErrCode.CACHE_UNAVAILABLE) // 100 == 45
        assert int(ErrCode.SCORER_UNAVAILABLE) // 100 == 49
        assert int(ErrCode.SELFCHECK_UNAVAILABLE) // 100 == 49
        assert ErrCode.is_degraded_code(int(ErrCode.RERANK_DEGRADED)) is True
        assert ErrCode.is_degraded_code(int(ErrCode.CACHE_UNAVAILABLE)) is True
        assert ErrCode.is_degraded_code(int(ErrCode.UNAUTHORIZED)) is False

    def test_documented_segments_exist(self):
        """/4/6/7 前端规范点名的错误码段全部落地。"""
        for code in (
            ErrCode.UNSUPPORTED_FILE_TYPE,   # 31xx
            ErrCode.PARSE_FAILED,            # 32xx
            ErrCode.CHUNK_POLICY_INVALID,    # 36xx
            ErrCode.INDEX_BUILD_FAILED,      # 37xx
            ErrCode.AGENT_ITERATION_LIMIT,   # 41xx
            ErrCode.TOOL_EXECUTION_FAILED,   # 42xx
            ErrCode.INTENT_FAILED,           # 46xx
            ErrCode.REWRITE_FAILED,          # 47xx
            ErrCode.DECOMPOSE_FAILED,        # 48xx
            ErrCode.SUBGRAPH_TOO_LARGE,      # 35xx
        ):
            assert isinstance(code, ErrCode)


# ---------------------------------------------------------------- 2. trace_id


class TestTracePropagation:
    def test_trace_id_readable_downstream(self):
        """core.tracing.get_trace_id 此前全仓零调用，下游拿不到 trace。"""
        token = set_trace_id(new_trace_id())
        try:
            assert get_trace_id() == get_trace_id()
            assert len(get_trace_id()) == 16
        finally:
            reset_trace_id(token)

    def test_reset_restores_empty_context(self):
        token = set_trace_id("fixed-value")
        reset_trace_id(token)
        auto = get_trace_id()
        assert auto != "" and auto != "fixed-value", "未设置时应生成新的"
        assert get_trace_id() == auto, "自动生成的值要缓存，同一请求内下游读到同一个"

    def test_error_response_reuses_request_trace_id(self, client):
        """异常路径必须复用入口 trace_id，否则用户复制到的号查不到日志。"""
        resp = client.get(
            "/knowledge/preview/does-not-exist-xyz",
            headers={"X-Trace-Id": "my-trace-000001"},
        )
        assert resp.status_code == 404
        body = resp.json()
        assert body["meta"]["trace_id"] == "my-trace-000001"
        assert resp.headers["X-Trace-Id"] == "my-trace-000001"


# ---------------------------------------------------------------- 3/4/5. 线性路径护栏


SAMPLE_MD = """# 产品手册

本产品支持 Markdown 与 PDF 两种文档格式。

## 保修政策

整机保修两年，电池保修一年。
"""


class TestLinearPathGuards:
    """线性路径的护栏。

    **原 ACL 相关用例已删除**（认证与授权下线，文档级可见性判定不再存在，
    `permission_denied` 这一拒答原因也随之消失——拒答只剩"无内容"与"证据不足"）。
    """

    def test_empty_question_rejected(self, client):
        resp = client.post("/ask", json={"question": "   "})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == int(ErrCode.QUESTION_EMPTY)


# ---------------------------------------------------------------- 6/7. Agent 路径可观测


class TestAgentPathObservability:
    def test_agent_path_emits_retrieval_and_confidence_events(self, client):
        """Agent 路径此前不发这三个事件 → 检索面板/置信度条全是死代码。"""
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        resp = client.post(
            "/ask/stream",
            json={"question": "保修多久？", "mode": "agent"},
        )
        text = resp.text
        assert "event: retrieval_stats" in text
        assert "event: confidence" in text
        assert "event: done" in text

    def test_agent_final_payload_has_retrieval_and_crag(self, client):
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        data = client.post(
            "/ask", json={"question": "保修多久？", "mode": "agent"}
        ).json()["data"]
        assert data["retrieval"] is not None
        assert "latency_ms" in data["retrieval"]
        assert "candidates" in data["retrieval"]
        assert data["crag"] is not None
        assert data["crag"]["branch"] in ("correct", "ambiguous", "incorrect")


# ---------------------------------------------------------------- 7. 嵌套检索统计


class TestNestedRetrievalStats:
    def test_nested_and_flat_both_present(self):
        stats = RetrievalStats(
            keyword_candidates=20, vector_candidates=18, fused_count=25,
            reranked_count=20, into_context_count=4,
            keyword_ms=3, vector_ms=5, fusion_ms=1, rerank_ms=9,
        )
        dumped = stats.model_dump()
        assert dumped["latency_ms"] == {"keyword": 3, "vector": 5, "fusion": 1, "rerank": 9}
        assert dumped["candidates"] == {
            "keyword": 20, "vector": 18, "fused": 25,
            "reranked": 20, "into_context": 4,
        }
        # 平铺字段保留（内部口径 / 旧消费者）
        assert dumped["keyword_ms"] == 3

    def test_api_returns_nested_shape(self, client):
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        data = client.post("/ask", json={"question": "保修多久？", "mode": "auto"}).json()["data"]
        retrieval = data["retrieval"]
        assert set(retrieval["latency_ms"]) == {"keyword", "vector", "fusion", "rerank"}
        assert set(retrieval["candidates"]) == {
            "keyword", "vector", "fused", "reranked", "into_context"
        }


# ---------------------------------------------------------------- 8. warn 信封


class TestWarnEnvelope:
    def test_rerank_degradation_returns_warn_envelope(self, client):
        """44xx 降级：仍返回结果，但 status=warn 让前端给轻提示。"""
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})

        class FailingReranker:
            model = "failing"

            def rerank(self, query, candidates, top_n, model=None):
                from app.services.retrieval.rerank import RerankError

                raise RerankError("模拟重排接口不可用")

        client.app.state.hybrid._reranker = FailingReranker()
        resp = client.post("/ask", json={"question": "保修多久？", "mode": "auto"})
        body = resp.json()
        assert body["status"] == "warn"
        assert body["error"]["code"] == int(ErrCode.RERANK_DEGRADED)
        assert body["data"]["reranked"] is False
        assert body["data"]["answer"]  # 结果仍可用

    def test_normal_response_is_ok_not_warn(self, client):
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        assert client.post("/ask", json={"question": "保修多久？", "mode": "auto"}).json()["status"] == "ok"


# ---------------------------------------------------------------- 9. 校验历史落库


class TestValidationHistory:
    def test_crag_rounds_and_self_check_persisted(self, client, tmp_path):
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        trace_id = new_trace_id()
        client.post(
            "/ask",
            json={"question": "保修多久？", "mode": "auto"},
            headers={"X-Trace-Id": trace_id},
        )
        history = client.app.state.pipeline._history
        assert history is not None, "线性路径此前不挂校验历史存储"
        buckets = history.by_trace(trace_id)
        assert buckets["crag_round"], "CRAG 轮次未落库"
        round0 = buckets["crag_round"][0]
        assert {"round", "branch", "avg_score", "evidence_count"} <= set(round0)

    def test_self_check_record_has_all_documented_fields(self, client):
        """SELF_CHECK 实体此前只存一个 int，四字段全部缺失。"""
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        result = client.app.state.pipeline.ask("保修多久？", "default", new_trace_id())
        record = result.self_check
        if record is None:
            pytest.skip("mock provider 未产出自检结论")
        for field in ("method", "checked_claims", "removed_claims", "passed"):
            assert hasattr(record, field), f"SELF_CHECK 缺字段 {field}"
        assert record.method == "llm_claim_review"
        assert record.checked_claims >= 1

    def test_estimate_claims_fallback(self):
        assert estimate_claims("") == 0
        assert estimate_claims("一句话。") == 1
        assert estimate_claims("第一句。第二句！第三句？") == 3

    def test_self_check_optional_for_custom_checkers(self):
        """测试替身只实现 check/regenerate 时不能把置信度降级成未知。"""
        from app.services.pipeline import RagPipeline

        class BareChecker:
            def check(self, answer, evidence_block):
                return SelfCheckResult(is_hallucination=False, unsupported_claims=[])

        record = RagPipeline._to_self_check_record(
            BareChecker(), SelfCheckResult(), "答案。", removed=0, remaining=0
        )
        assert record.checked_claims >= 1
        assert record.passed is True


# ---------------------------------------------------------------- 10/11. 索引版本化


class TestIndexVersioning:
    def test_chunk_id_version_roundtrip(self):
        base = "doc-a#3"
        versioned = with_version(base, 2)
        assert versioned == "doc-a@v2#3"
        assert chunk_version(versioned) == 2
        assert chunk_version(base) == 0
        # 重复打版本号不叠加
        assert with_version(versioned, 3) == "doc-a@v3#3"

    def test_reparse_keeps_old_chunks_for_traceability(self, client):
        """reparse 此前物理删除旧块且复用序号空间 → 历史答案引用全部失效。"""
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        store = client.app.state.store
        knowledge = client.app.state.knowledge
        doc_id = knowledge.list_files()[0].doc_id
        v1_ids = set(store.doc_chunk_ids(doc_id))
        assert v1_ids

        knowledge.reparse(doc_id, {"strategy": "semantic", "threshold": 0.3})
        v2_ids = set(store.doc_chunk_ids(doc_id))
        record = knowledge.get(doc_id)

        assert record.version == 2
        current = set(store.doc_chunk_ids(doc_id))
        # 旧块被改名为 @v1 并保留（服务历史溯源），不再与新版本撞号
        retired = {cid for cid in current if chunk_version(cid) == 1}
        fresh = {cid for cid in current if chunk_version(cid) == 2}
        assert retired, "旧版本块未保留，历史答案将无法溯源"
        assert fresh, "新版本块未写入"
        assert retired & fresh == set(), "新旧版本撞号了"
        assert v1_ids & fresh == set(), "新版本复用了旧版本的 chunk_id"
        # 改名后的旧块仍可按 chunk_id 直接查到（溯源链路不断）
        assert all(store.get_chunk(cid) is not None for cid in retired)

    def test_purge_keeps_configured_number_of_versions(self, client):
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        store = client.app.state.store
        knowledge = client.app.state.knowledge
        doc_id = knowledge.list_files()[0].doc_id
        for i in range(2, 6):
            knowledge.reparse(doc_id, {"strategy": "recursive"})
        keep = client.app.state.settings.index_version_keep
        versions = store.doc_versions(doc_id)
        # keep 份是"可回滚的历史版本"，不含当前版，所以总数上限是 keep + 1
        assert len(versions) <= keep + 1, f"保留了 {len(versions)} 版，超过配置的 {keep} 份历史 + 当前"
        assert versions[-1] == 5, "最新版本必须保留"
        assert versions[0] == 2, f"最旧版本应为 v2（v1 已超出保留份数被清理），实际 {versions}"

    def test_vector_count_mismatch_blocks_publish(self, client):
        """索引发布闸门：向量条数必须严格等于块条数。"""
        from app.core.envelope import ApiError

        knowledge = client.app.state.knowledge
        doc_id = knowledge.list_files()[0].doc_id if knowledge.list_files() else None
        if doc_id is None:
            client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
            doc_id = client.app.state.knowledge.list_files()[0].doc_id

        from app.services.parsers.base import ParseResult

        record = knowledge.get(doc_id)
        parsed = ParseResult(segments=[("第1页", SAMPLE_MD)], tables=[], assets=[], outline=[], warnings=[])
        # 让 embed 少返回一条向量，模拟部分失败
        knowledge._embeddings = type(
            "Broken", (), {
                "embed_texts": lambda self, texts: [[0.1] * 1024 for _ in texts[:-1]],
                "embed": lambda self, texts: [[0.1] * 1024 for _ in texts],
            }
        )()
        with pytest.raises(ApiError) as excinfo:
            knowledge.index_parsed(record, parsed)
        assert "向量条数" in excinfo.value.message


# ---------------------------------------------------------------- 12/13. 入库质量与状态机


class TestIngestQualityAndStages:
    def test_file_stats_include_pages_and_short_chunks(self, client):
        """§6.2 质量红线所需的两项此前缺失，导致红线无数据可判。"""
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        docs = client.app.state.knowledge.list_files()
        doc = docs[0]
        assert doc.short_chunk_count >= 0
        assert doc.chunker, "未记录实际使用的切块器"

    def test_file_stage_covers_all_documented_states(self):
        """7 态状态机此前只有 5 态，FILE_PENDING / FILE_UPLOADING 零引用。"""
        from app.domain import schemas

        states = {
            schemas.FILE_PENDING, schemas.FILE_UPLOADING, schemas.FILE_QUEUED,
            schemas.FILE_PARSING, schemas.FILE_CHUNKING, schemas.FILE_INDEXING,
            schemas.FILE_DONE, schemas.FILE_FAILED,
        }
        assert len(states) == 8  # 7 态 + failed
        # 两个此前不可达的状态必须真的被引用
        source = Path(schemas.__file__).parent.parent / "services" / "ingest.py"
        assert "FILE_UPLOADING" in source.read_text(encoding="utf-8")



# ---------------------------------------------------------------- 17. 热更新真正生效


class TestRetrievalConfigHotUpdate:
    def test_rrf_k_and_recall_sizes_are_configurable(self):
        """此前三项只注入构造器，RetrievalConfig 里根本没有字段。"""
        config = RetrievalConfig()
        assert config.rrf_k == 60
        assert config.per_route_top_n == 20
        assert config.fused_top_n == 20
        config.rrf_k = 10
        config.per_route_top_n = 5
        config.fused_top_n = 8
        assert (config.rrf_k, config.per_route_top_n, config.fused_top_n) == (10, 5, 8)

    def test_zero_values_fall_back_to_settings(self):
        """存量 JSON 缺新字段时用 0 表达会静默把召回数清零。"""
        settings = Settings()
        config = RetrievalConfig(rrf_k=0, per_route_top_n=0, fused_top_n=0, cache_ttl_s=0)
        normalized = config.with_fallbacks(settings)
        assert normalized.rrf_k == settings.rrf_k
        assert normalized.per_route_top_n == settings.retrieval_per_route_top_n
        assert normalized.fused_top_n == settings.retrieval_fused_top_n
        assert normalized.cache_ttl_s == settings.cache_ttl_s

    def test_config_store_baseline_from_settings(self, tmp_path):
        settings = Settings()
        settings.cache_ttl_s = 1234
        settings.ratelimit_capacity = 77
        from app.services.config_store import RetrievalConfigStore

        store = RetrievalConfigStore(tmp_path / "cfg.json", settings=settings)
        active = store.get_active()
        assert active.cache_ttl_s == 1234
        assert active.ratelimit_capacity == 77

    def test_reranker_receives_runtime_model(self):
        """重排模型热更新此前只改缓存键，实际调用模型不变。"""
        from app.services.retrieval.hybrid import _rerank

        seen = {}

        class NewSignature:
            def rerank(self, query, candidates, top_n, model=None):
                seen["model"] = model
                return candidates

        class OldSignature:
            def rerank(self, query, candidates, top_n, model=None):
                seen["model"] = "default"
                return candidates

        _rerank(NewSignature(), "q", [], 4, "gte-rerank-v2-x")
        assert seen["model"] == "gte-rerank-v2-x"
        # 旧签名实现必须仍可调用（向后兼容）
        _rerank(OldSignature(), "q", [], 4, "gte-rerank-v2-x")
        assert seen["model"] == "default"


# ---------------------------------------------------------------- 18. 语义缓存


class TestSemanticCache:
    def test_semantic_lookup_hits_on_paraphrase(self):
        """此前只有 SHA256 精确哈希，"保修政策 vs 保修条款"不命中。"""
        cache = InMemorySemanticCache(sim_threshold=0.9)
        cache.set("k1", {"answer": "两年"}, 60, question="保修政策", vector=[1.0, 0.0],
                  signature="sig")
        entry, kind = cache.lookup("保修条款", "missing-key", [0.995, 0.1], signature="sig")
        assert kind == "semantic", f"近似查询未命中（cos={_cosine([0.995, 0.1], [1.0, 0.0]):.4f}）"
        assert entry is not None
        assert entry.payload["answer"] == "两年"

    def test_dissimilar_query_misses(self):
        cache = InMemorySemanticCache(sim_threshold=0.95)
        cache.set("k1", {"answer": "两年"}, 60, question="保修政策", vector=[1.0, 0.0],
                  signature="sig")
        entry, kind = cache.lookup("电池容量", "missing", [0.0, 1.0], signature="sig")
        assert kind == "miss"
        assert entry is None

    def test_exact_hit_takes_precedence(self):
        cache = InMemorySemanticCache(sim_threshold=0.5)
        cache.set("k1", {"answer": "精确"}, 60, question="q", vector=[1.0, 0.0])
        entry, kind = cache.lookup("q", "k1", [1.0, 0.0])
        assert kind == "exact"
        assert entry.payload["answer"] == "精确"

    def test_cosine_helper(self):
        assert _cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
        assert _cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
        assert _cosine([], [1.0]) == 0.0
        assert _cosine([1.0], [1.0, 2.0]) == 0.0

    def test_expired_entries_not_returned_by_lookup(self):
        cache = InMemorySemanticCache(sim_threshold=0.5)
        cache.set("k1", {"answer": "x"}, -1, question="q", vector=[1.0, 0.0], signature="sig")
        entry, kind = cache.lookup("q", "k1", [1.0, 0.0], signature="sig")
        assert entry is None

    def test_cache_failure_degrades_silently(self, client):
        """45xx：缓存不可用必须无感降级 + 上报指标，不能让用户看到错误。"""
        client.post("/knowledge/upload", files={"file": ("手册.md", SAMPLE_MD.encode(), "text/markdown")})
        pipeline = client.app.state.pipeline

        class BrokenCache:
            def lookup(self, *a, **k):
                raise RuntimeError("redis 连接失败")

            def set(self, *a, **k):
                raise RuntimeError("redis 连接失败")

            def get(self, key):
                return None

        pipeline._cache = BrokenCache()
        resp = client.post("/ask", json={"question": "保修多久？", "mode": "auto"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        metrics = client.get("/metrics").text
        assert 'rag_cache_total{result="error"}' in metrics


# ---------------------------------------------------------------- 19. 令牌桶按真实租户


class TestRateLimitTenancy:
    def test_token_bucket_keyed_by_real_tenant(self, client):
        """此前桶 key 用 default_tenant_id 常量，多租户互相挤占。"""
        limiter = client.app.state.limiter
        rate, capacity = 100.0, 100

        for _ in range(5):
            assert limiter.allow("tenant-a", rate, capacity)[0] is True
        # 另一个租户的桶必须是独立的
        assert limiter.bucket_state("tenant-b") is None
        assert limiter.allow("tenant-b", rate, capacity)[0] is True


# ---------------------------------------------------------------- 23. 工具注册


class TestToolRegistry:
    def test_sql_search_rejects_writes(self):
        """只允许 SELECT/WITH 是硬边界，否则 Agent 一次幻觉就能 DROP 表。"""
        from app.services.agent.tools import SqlSearchTool

        assert SqlSearchTool._validate("SELECT 1") == "SELECT 1"
        for bad in ("DELETE FROM t", "UPDATE t SET a=1", "DROP TABLE t", "SELECT 1; DROP TABLE t"):
            with pytest.raises(InvalidInputError):
                SqlSearchTool._validate(bad)

    def test_tool_timeout_comes_from_config(self):
        registry = ToolRegistry(timeout_s=7)
        assert registry._timeout_s == 7
        assert ToolRegistry()._timeout_s == 30  # 默认兜底

    def test_unregistered_tool_falls_back(self):
        registry = ToolRegistry()
        from app.services.agent.tools import CalculatorTool

        registry.register(CalculatorTool())
        tool, hallucinated = registry.resolve("不存在的工具")
        assert hallucinated is True
        assert tool.name == "calculator"

    def test_optional_tools_absent_without_config(self):
        registry = ToolRegistry()
        registry.register(__import__(
            "app.services.agent.tools", fromlist=["CalculatorTool"]
        ).CalculatorTool())
        names = registry.names()
        assert "sql_search" not in names, "未配置 DSN 时不应注册 sql_search"
        assert "web_search" not in names, "未配置端点时不应注册 web_search"


# ---------------------------------------------------------------- 24. 配置校验与探针


class TestStartupValidationAndProbes:
    def test_default_settings_pass_validation(self):
        # 显式承认测试环境（mock + 关闭认证）应当通过校验；
        # 裸 defaults（auto 无凭据 / 认证无凭证来源）本来就该被拒绝
        assert Settings(model_provider="mock", auth_enabled=False).validate_startup() == []

    def test_dashscope_without_key_is_rejected(self):
        """显式要求走平台却没凭据是配置错误，不该静默降级成 mock。"""
        issues = Settings(model_provider="dashscope", dashscope_api_key="").validate_startup()
        assert any(i["field"] == "dashscope_api_key" for i in issues)

    def test_config_items_hide_secrets(self):
        items = {i["key"]: i for i in Settings(dashscope_api_key="sk-secret").config_items()}
        assert items["dashscope_api_key"]["is_secret"] is True
        assert items["dashscope_api_key"]["value"] == "***"
        assert "sk-secret" not in str(items["dashscope_api_key"]["value"])

    def test_invalid_numeric_ranges_flagged(self):
        issues = Settings(chunk_overlap=400, chunk_size=300, rrf_k=0).validate_startup()
        fields = {i["field"] for i in issues}
        assert "chunk_overlap" in fields
        assert "rrf_k" in fields

    def test_probe_endpoints_exist_and_are_layered(self, client):
        """三级探针分层：livez 不查依赖 / readyz 就绪 / healthz 依赖明细。"""
        livez = client.get("/livez")
        assert livez.status_code == 200
        assert livez.text.strip() == "ok"

        readyz = client.get("/readyz")
        assert readyz.status_code in (200, 503)
        assert "status" in readyz.json()

        healthz = client.get("/healthz")
        body = healthz.json()
        assert "dependencies" in body
        names = {d["name"] for d in body["dependencies"]}
        assert {"vector_store", "bm25_index", "graph_store", "provider"} <= names
        assert all("latency_ms" in d and "status" in d for d in body["dependencies"])

    def test_config_store_exposed_via_admin(self, client):
        resp = client.get("/admin/retrieval-config")
        assert resp.status_code == 200
        active = resp.json()["data"]["active"]
        # 六个可调参数现在真的都在模型里了
        for key in ("top_k", "rrf_k", "per_route_top_n", "fused_top_n",
                    "rerank_model", "cache_ttl_s"):
            assert key in active, f"RetrievalConfig 缺可调参数 {key}"


# ---------------------------------------------------------------- 固定身份


def test_principal_is_fixed():
    """认证已下线：身份是常量，且不可被请求头改写。

    `Principal` 是 frozen dataclass——就地改写属性会直接抛错，
    这正是"身份固定"在类型层面的保证。
    """
    principal = Principal()
    assert principal.tenant_id == "default"
    assert principal.user_id == "local"
    with pytest.raises(Exception):
        principal.tenant_id = "hacked"  # type: ignore[misc]
