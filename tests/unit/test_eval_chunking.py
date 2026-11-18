"""切块策略实验与锚点解析的测试（批次 1）。

两条关键性质：
1. **标注与切块方式无关**（锚点而非 chunk_id）→ 同一份标注能跨策略比较；
2. **实验测的是生产路径**（走 `apply_chunk_policy` 工厂，不是实验自己实现的切块）。
"""
import pytest

from app.services.evaluation.dataset import resolve_anchors, unresolved_queries
from app.services.evaluation.local_eval import (
    build_retriever,
    run_queries,
    score_runs,
)
from app.core.errors import ConfigError, InvalidInputError
from app.services.evaluation.policies import (
    PRESETS,
    chunk_corpus,
    resolve_policy,
)

EVAL_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / "data" / "eval"


class TestResolvePolicy:
    def test_preset_name(self):
        resolved = resolve_policy("recursive-500-100")
        assert resolved["name"] == "recursive-500-100"
        assert resolved["chunk_size"] == 500
        assert resolved["overlap"] == 100
        assert resolved["policy"]["strategy"] == "recursive"

    def test_default(self):
        assert resolve_policy("")["name"] == "recursive-300-50"

    def test_json_spec(self):
        resolved = resolve_policy('{"strategy":"semantic","params":{"threshold":0.7}}')
        assert resolved["policy"]["strategy"] == "semantic"
        assert resolved["policy"]["params"]["threshold"] == 0.7

    def test_unknown_preset_raises(self):
        with pytest.raises((ConfigError, InvalidInputError)):
            resolve_policy("no-such-policy")

    def test_broken_json_raises(self):
        # 2026-11-12（`A-07`）：策略 JSON 解析失败 → `ConfigError`（配置非法，500），
        # 不再是裸 `ValueError`（没有错误码，调用方无法区分「配置坏了」与「参数错了」）。
        with pytest.raises(ConfigError):
            resolve_policy("{not json")


class TestChunkCorpus:
    def test_bigger_chunks_fewer_blocks(self):
        """块放大 → 块数下降（实验的核心事实之一）。"""
        small = chunk_corpus(EVAL_DIR / "corpus", "recursive-300-50", "eval-tenant")
        big = chunk_corpus(EVAL_DIR / "corpus", "recursive-800-100", "eval-tenant")
        assert len(big) < len(small)

    def test_every_preset_produces_chunks(self):
        for name in PRESETS:
            chunks = chunk_corpus(EVAL_DIR / "corpus", name, "eval-tenant")
            assert chunks, f"{name} 没有产出块"
            assert all(c.content for c in chunks)

    def test_chunk_ids_are_stable(self):
        """同一策略两次切块的 chunk_id 必须一致（否则标注无法复现）。"""
        a = chunk_corpus(EVAL_DIR / "corpus", "recursive-300-50", "eval-tenant")
        b = chunk_corpus(EVAL_DIR / "corpus", "recursive-300-50", "eval-tenant")
        assert [c.chunk_id for c in a] == [c.chunk_id for c in b]
        assert [c.content for c in a] == [c.content for c in b]


class TestResolveAnchors:
    def _chunks(self):
        return chunk_corpus(EVAL_DIR / "corpus", "recursive-300-50", "eval-tenant")

    def test_anchor_maps_to_containing_chunk(self):
        chunks = self._chunks()
        resolved = resolve_anchors({"q1": {"默认是 Docker Compose": 2}}, chunks)
        assert "q1" in resolved
        cid = list(resolved["q1"])[0]
        target = next(c for c in chunks if c.chunk_id == cid)
        assert "默认是 Docker Compose" in target.content

    def test_picks_shortest_matching_chunk(self):
        """锚点落在重叠区的两块时取最短的（最贴近"独立足以回答"）。"""
        chunks = self._chunks()
        resolved = resolve_anchors({"q1": {"默认是 Docker Compose": 2}}, chunks)
        cid = list(resolved["q1"])[0]
        target = next(c for c in chunks if c.chunk_id == cid)
        matches = [c for c in chunks if "默认是 Docker Compose" in c.content]
        assert len(target.content) == min(len(c.content) for c in matches)

    def test_missing_anchor_drops_the_query(self):
        """锚点找不到 → 整条丢弃（不记 0：那是"标注失效"不是"召回失败"）。"""
        chunks = self._chunks()
        resolved = resolve_anchors({"q1": {"这句话根本不在语料里": 2}}, chunks)
        assert resolved == {}
        assert unresolved_queries({"q1": {"这句话根本不在语料里": 2}}, resolved) == ["q1"]

    def test_grade_takes_max_when_same_chunk_hit(self):
        """多个锚点落进同一块时取**最高** grade（更严格的判定优先）。"""
        chunks = self._chunks()
        anchors = {"q1": {"Docker Compose": 1, "默认是 Docker Compose": 2}}
        resolved = resolve_anchors(anchors, chunks)
        assert len(resolved["q1"]) == 1, "两个锚点应落在同一块"
        assert max(resolved["q1"].values()) == 2


class TestLocalEval:
    def test_end_to_end_scores_are_sane(self):
        chunks = chunk_corpus(EVAL_DIR / "corpus", "recursive-300-50", "eval-tenant")
        retriever = build_retriever(chunks)
        queries = [{"query_id": "q1", "question": "Milvus 分页拉取上限是多少？"}]
        runs = run_queries(retriever, queries, "eval-tenant", top_k=10)
        assert "q1" in runs
        assert runs["q1"]["ranked"], "检索结果不能为空"
        assert "keyword@original" in runs["q1"]["routes"]

        anchors = {"q1": {"超过 16k 块后静默只取前 16k": 2}}
        qrels = resolve_anchors(anchors, chunks)
        report = score_runs(runs, qrels, (5, 10, 20))
        agg = report["aggregate"]
        assert agg["queries"] == 1
        assert 0.0 <= agg["recall"]["10"] <= 1.0
        assert "attribution" in report

    def test_debug_routes_expose_both_paths(self):
        chunks = chunk_corpus(EVAL_DIR / "corpus", "recursive-300-50", "eval-tenant")
        retriever = build_retriever(chunks)
        runs = run_queries(
            retriever, [{"query_id": "q1", "question": "令牌桶容量"}], "eval-tenant"
        )
        routes = runs["q1"]["routes"]
        assert any(r.startswith("keyword") for r in routes)
        assert any(r.startswith("vector") for r in routes)
