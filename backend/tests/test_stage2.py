"""阶段 2 功能测试：混合检索（BM25/RRF/Rerank）+ 缓存 / 限流 / 指标。

- RRF 数值自检（README §3.3 的三条排序语义）；
- BM25 中文分词与零分过滤（编号型 query 召回）；
- 混合检索统计与证据 source_route；
- 语义缓存：命中、完整响应、键随模型/租户变化；
- 令牌桶限流：429 + Retry-After；
- /metrics 指标暴露、admin 端点、单文档检索诊断、重排降级。
"""
import pytest

from app.domain.schemas import Chunk
from app.services.cache import make_cache_key
from app.services.ratelimit import TokenBucketLimiter
from app.services.retrieval.base import RetrievalCandidate
from app.services.retrieval.fusion import rrf_fuse
from app.services.retrieval.rerank import RerankError
from tests.conftest import upload_sample


def _chunk(cid: str, content: str = "x") -> Chunk:
    doc_id = cid.split("#")[0]
    return Chunk(
        chunk_id=cid, doc_id=doc_id, seq=int(cid.split("#")[1]),
        content=content, tenant_id="t", source="全文",
    )


def _cand(cid: str, score: float = 1.0) -> RetrievalCandidate:
    return RetrievalCandidate(chunk=_chunk(cid), score=score)


# ---------- RRF 数值自检（README §3.3）----------

class TestRRF:
    def test_single_route_rank1_score(self):
        fused = rrf_fuse([("keyword", [_cand("d#1")])], k=60)
        assert fused[0].rrf_score == pytest.approx(1 / 61, rel=1e-3)

    def test_both_routes_rank1_beats_single_route_rank1(self):
        both = rrf_fuse([("keyword", [_cand("d#1")]), ("vector", [_cand("d#1")])], k=60)
        single = rrf_fuse([("keyword", [_cand("d#2")])], k=60)
        assert both[0].rrf_score == pytest.approx(2 / 61, rel=1e-3)
        assert both[0].rrf_score > single[0].rrf_score
        assert both[0].source_route == "both"

    def test_both_routes_mid_rank_beats_single_route_rank1(self):
        """两路同时靠前（A3+B3 ≈ 0.0317）胜过单路极靠前（A1 = 0.0164）。"""
        mid = rrf_fuse(
            [("keyword", [_cand("x#1"), _cand("x#2"), _cand("d#1")]),
             ("vector", [_cand("y#1"), _cand("y#2"), _cand("d#1")])],
            k=60,
        )
        single = rrf_fuse([("keyword", [_cand("d#2")])], k=60)
        mid_score = next(c.rrf_score for c in mid if c.chunk.chunk_id == "d#1")
        assert mid_score > single[0].rrf_score

    def test_weights_change_fusion_order(self):
        routes = [("keyword", [_cand("d#1")]), ("vector", [_cand("d#2")])]
        fused = rrf_fuse(routes, k=60, weights={"keyword": 2.0, "vector": 1.0})
        assert fused[0].chunk.chunk_id == "d#1"


# ---------- BM25 ----------

SERIAL_MD = """# 产品规格书

XB-2024 电池的标称电压为 24V，容量 5000mAh，工作温度范围零下 20 度到 60 度。

XB-2025 电池的标称电压为 36V，容量 6000mAh，支持快充协议。

充电器 CD-100 支持 100W 输出，兼容 XB-2024 与 XB-2025 两款电池。
"""

# 多块长文档：每段约 250 字，切块后应产生 3 个以上块
LONG_MD = """# 电池产品白皮书

## 第一章 电池规格

XB-2024 电池的标称电压为 24V，容量 5000mAh，采用磷酸铁锂电芯，循环寿命超过 2000 次。
该电池的工作温度范围为零下 20 度到 60 度，支持低温自加热功能，适合北方地区冬季使用。
电池内置智能管理芯片，可以实时监测电压、电流与温度，并在异常时自动断电保护。

## 第二章 充电规范

XB-2024 电池推荐使用 CD-100 充电器进行充电，充电电流不得超过 10A。
充电环境温度应保持在 0 度到 45 度之间，低于 0 度时充电器会自动降低电流以保护电芯。
完整充电时间约为 2 小时，电量从 20% 充至 80% 仅需 40 分钟。
禁止在潮湿环境或易燃物附近充电，充电完成后应及时断开充电器。

## 第三章 保养与存放

长期存放电池时，应将电量保持在 40% 到 60% 之间，并放置于阴凉干燥处。
每三个月应进行一次充放电循环，以防止电芯活性下降。
电池外壳出现鼓包、漏液或异味时应立即停止使用，并联系售后处理，切勿自行拆解。

## 第四章 保修政策

XB-2024 电池享受两年免费保修，保修范围涵盖电芯容量衰减超过 20% 的情况。
人为拆解、进水、跌落造成的损坏不在保修范围内。
保修期内更换的电池延续原保修期限，不重新计算。
"""


class TestBM25:
    def test_serial_number_query_recalls_exact_chunk(self, client):
        """编号 / 型号召回：阶段 1 的痛点，BM25 精确匹配解决。"""
        client.post("/knowledge/upload", files={"file": ("规格书.md", SERIAL_MD.encode(), "text/markdown")})
        resp = client.post("/ask", json={"question": "XB-2024 的电压是多少？"})
        data = resp.json()["data"]
        top = data["evidence"][0]
        assert "XB-2024" in top["content"]
        assert "24V" in top["content"]
        # 检索统计齐全：四个计数 + 四段耗时
        stats = data["retrieval"]
        assert stats["keyword_candidates"] >= 1
        assert stats["vector_candidates"] >= 1
        assert stats["into_context_count"] == len(data["evidence"])

    def test_zero_score_filtered_in_keyword_route(self, client):
        """零分文档过滤（检索器单元层）：无关查询任何词都不命中 → 0 候选，不进入 RRF。"""
        from app.domain.schemas import Chunk
        from app.services.retrieval.bm25 import BM25Retriever

        bm25 = BM25Retriever()
        bm25.rebuild([
            Chunk(chunk_id="d#1", doc_id="d", seq=1, content="XB-2024 电池的标称电压为 24V", tenant_id="t", source="全文"),
            Chunk(chunk_id="d#2", doc_id="d", seq=2, content="充电器 CD-100 支持 100W 输出", tenant_id="t", source="全文"),
        ])
        assert bm25.search("zzqqxxvvkkhhr", 10, "t") == []
        # 命中词存在时正常返回且零分文档被过滤（只有 1 条相关）
        hits = bm25.search("电池 电压", 10, "t")
        assert len(hits) == 1 and hits[0].chunk.chunk_id == "d#1"

    def test_evidence_has_source_route(self, client):
        client.post("/knowledge/upload", files={"file": ("规格书.md", SERIAL_MD.encode(), "text/markdown")})
        data = client.post("/ask", json={"question": "XB-2025 支持快充吗？"}).json()["data"]
        routes = {e["source_route"] for e in data["evidence"]}
        assert routes <= {"keyword", "vector", "both"}
        assert data["reranked"] is True


# ---------- 语义缓存 ----------

class TestSemanticCache:
    def test_second_identical_ask_hits_cache(self, client):
        upload_sample(client)
        first = client.post("/ask", json={"question": "支持哪些文档格式？"}).json()["data"]
        assert first["from_cache"] is False
        second = client.post("/ask", json={"question": "支持哪些文档格式？"}).json()["data"]
        assert second["from_cache"] is True
        # 命中缓存时响应结构与新鲜请求同构（证据完整，不是只有答案字符串）
        assert second["answer"] == first["answer"]
        assert len(second["evidence"]) == len(first["evidence"])

    def test_stream_cache_hit_still_emits_full_events(self, client):
        upload_sample(client)
        client.post("/ask/stream", json={"question": "计费规则是什么？"})
        with client.stream("POST", "/ask/stream", json={"question": "计费规则是什么？"}) as resp:
            text = "".join(resp.iter_text())
        assert "event: evidence" in text
        assert '"from_cache": true' in text
        assert "event: done" in text

    def test_cache_key_varies_with_tenant_and_model(self):
        k1 = make_cache_key("t1", "q", "m1", "c")
        k2 = make_cache_key("t2", "q", "m1", "c")  # 租户不同 → 防跨租户串答案
        k3 = make_cache_key("t1", "q", "m2", "c")  # 模型版本不同 → 换模型不命中旧答案
        k4 = make_cache_key("t1", "q", "m1", "c2")  # 配置不同
        assert len({k1, k2, k3, k4}) == 4


# ---------- 令牌桶限流 ----------

class TestRateLimit:
    def test_bucket_refill_never_exceeds_capacity(self):
        limiter = TokenBucketLimiter()
        import time as _time

        _time.sleep(0.05)
        for _ in range(5):
            allowed, _ = limiter.allow("t", rate_per_min=600.0, capacity=2)
        # 容量 2：高频下绝大多数请求应被拒
        allowed_count = 0
        for _ in range(5):
            ok, retry = limiter.allow("t", rate_per_min=1.0, capacity=2)
            allowed_count += int(ok)
            if not ok:
                assert retry >= 1
        assert allowed_count <= 2

    def test_api_returns_429_with_retry_after(self, client):
        upload_sample(client)
        # 把限流配置调小（测试环境延迟生效为 0）
        resp = client.put("/admin/retrieval-config", json={
            "vector_enabled": True, "bm25_enabled": True,
            "vector_weight": 1.0, "bm25_weight": 1.0,
            "rerank_enabled": True, "rerank_model": "gte-rerank-v2",
            "top_k": 4, "cache_ttl_s": 3600,
            "ratelimit_per_min": 0.001, "ratelimit_capacity": 2,
        })
        assert resp.json()["status"] == "ok"
        codes = []
        retry_after = None
        for i in range(4):
            r = client.post("/ask", json={"question": f"问题 {i}"})
            codes.append(r.status_code)
            if r.status_code == 429:
                retry_after = r.headers.get("Retry-After")
        assert codes[:2] == [200, 200]
        assert 429 in codes[2:]
        assert retry_after is not None and int(retry_after) >= 1
        # 错误信封携带 61xx 错误码
        assert r.json()["error"]["code"] == 6101


# ---------- 指标与 admin 端点 ----------

class TestMetricsAndAdmin:
    def test_metrics_endpoint_exposes_core_metrics(self, client):
        upload_sample(client)
        client.post("/ask", json={"question": "支持哪些文档格式？"})
        text = client.get("/metrics").text
        for name in (
            "rag_requests_total",
            "rag_latency_seconds",
            "rag_cache_total",
            "rag_retrieval_top_score",
            "rag_llm_calls_total",
            "rag_retrieval_stage_ms",
        ):
            assert name in text, f"缺少指标 {name}"

    def test_admin_metrics_summary_and_top_queries(self, client):
        upload_sample(client)
        client.post("/ask", json={"question": "支持哪些文档格式？"})
        client.post("/ask", json={"question": "支持哪些文档格式？"})
        summary = client.get("/admin/metrics/summary").json()["data"]
        assert summary["total_requests"] >= 2
        assert 0.0 <= summary["cache_hit_rate"] <= 1.0
        top = client.get("/admin/metrics/top-queries").json()["data"]
        # TASK-182：指标层不再持有用户原话，只存哈希——同一问题哈希一致，
        # 因此"高频问题"依然可统计，但内存里不留明文副本。
        assert "query" not in top[0], "指标不应回传用户原话"
        assert top[0]["query_hash"], "应返回问题指纹"
        assert top[0]["count"] == 2
        # 指纹稳定：同一问题两次提问聚合成同一条
        assert len({r["query_hash"] for r in top}) == len(top)
        ts = client.get("/admin/metrics/timeseries?metric=requests").json()["data"]
        assert ts["metric"] == "requests" and len(ts["points"]) >= 1

    def test_retrieval_config_put_takes_effect(self, client):
        client.post("/knowledge/upload", files={"file": ("长文档.md", LONG_MD.encode(), "text/markdown")})
        before = client.post("/ask", json={"question": "电池的电压是多少？"}).json()["data"]
        assert len(before["evidence"]) > 2
        client.put("/admin/retrieval-config", json={
            "vector_enabled": True, "bm25_enabled": True,
            "vector_weight": 1.0, "bm25_weight": 1.0,
            "rerank_enabled": True, "rerank_model": "gte-rerank-v2",
            "top_k": 2, "cache_ttl_s": 3600,
            "ratelimit_per_min": 60, "ratelimit_capacity": 60,
        })
        after = client.post("/ask", json={"question": "电池的电压是多少？"}).json()["data"]
        # top_k 改为 2 后被采纳证据随之变化（未采纳证据也会返回，不计入）
        used = [e for e in after["evidence"] if e["used"]]
        assert len(used) == 2
        assert after["from_cache"] is False


# ---------- 检索诊断与降级 ----------

class TestProbeAndDegradation:
    def test_probe_returns_only_doc_chunks(self, client):
        upload_sample(client)
        client.post("/knowledge/upload", files={"file": ("规格书.md", SERIAL_MD.encode(), "text/markdown")})
        # doc_id 由文件名推导：产品手册.md → 产品手册
        resp = client.post("/knowledge/产品手册/probe", json={"query": "文档格式", "top_k": 3})
        data = resp.json()["data"]
        assert data["doc_id"] == "产品手册"
        assert all(h["chunk_id"].startswith("产品手册#") for h in data["hits"])
        assert len(data["hits"]) >= 1

    def test_rerank_failure_degrades_not_blocks(self, client):
        upload_sample(client)

        class FailingReranker:
            model = "failing"

            def rerank(self, query, candidates, top_n, model=None):
                raise RerankError("模拟重排接口不可用")

        client.app.state.hybrid._reranker = FailingReranker()
        resp = client.post("/ask", json={"question": "支持哪些文档格式？"})
        data = resp.json()["data"]
        # 增强能力降级：仍返回答案与证据，显式标记未精排
        assert resp.status_code == 200
        assert data["reranked"] is False
        assert data["evidence"]
        assert data["answer"]
