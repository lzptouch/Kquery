"""检索评测指标单测（批次 0）。

指标算错比指标缺失更危险——错的数字会被用来做参数决策。
所以这里的期望值都是**可手工验算**的常数，而不是"跑一遍看输出"的自证。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.evaluation import metrics as M
from app.services.evaluation.dataset import (
    load_qrels,
    load_queries,
    load_runs,
    save_runs,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "tests" / "data" / "eval"


class TestRecall:
    def test_full_hit(self):
        assert M.recall_at_k(["a", "b"], {"a", "b"}, 2) == 1.0

    def test_half_hit(self):
        assert M.recall_at_k(["a", "x"], {"a", "b"}, 2) == 0.5

    def test_k_cutoff(self):
        """k 是硬边界：命中在第 3 位时 recall@2 必须是 0。"""
        assert M.recall_at_k(["x", "y", "a"], {"a"}, 2) == 0.0
        assert M.recall_at_k(["x", "y", "a"], {"a"}, 3) == 1.0

    def test_no_relevant_returns_zero(self):
        """没有相关块时返回 0 而不是 1（该题对指标无贡献，由 n_relevant 识别）。"""
        assert M.recall_at_k(["a"], set(), 5) == 0.0

    def test_duplicate_hits_count_once(self):
        assert M.recall_at_k(["a", "a"], {"a", "b"}, 5) == 0.5


class TestNdcg:
    # grades: a=2, b=1；ranked=[b, a]
    # DCG@2 = (2^1-1)/log2(2) + (2^2-1)/log2(3) = 1 + 3/1.58496 = 2.892789
    # IDCG@2 = (2^2-1)/log2(2) + (2^1-1)/log2(3) = 3 + 1/1.58496 = 3.630930
    # nDCG = 2.892789 / 3.630930 = 0.796724
    GRADES = {"a": 2, "b": 1}

    def test_dcg_value(self):
        assert M.dcg_at_k(["b", "a"], self.GRADES, 2) == pytest.approx(2.892789, rel=1e-5)

    def test_idcg_value(self):
        assert M.idcg_at_k(self.GRADES, 2) == pytest.approx(3.630930, rel=1e-5)

    def test_ndcg_value(self):
        assert M.ndcg_at_k(["b", "a"], self.GRADES, 2) == pytest.approx(0.796724, rel=1e-4)

    def test_perfect_order_is_one(self):
        assert M.ndcg_at_k(["a", "b"], self.GRADES, 2) == pytest.approx(1.0)

    def test_no_relevant_is_zero(self):
        assert M.ndcg_at_k(["a"], {"a": 0}, 2) == 0.0

    def test_k_respected(self):
        """只取前 k 个位置：第 3 位的命中不计入 nDCG@2。"""
        assert M.dcg_at_k(["x", "y", "a"], self.GRADES, 2) == 0.0


class TestMrr:
    def test_first_position(self):
        assert M.mrr(["a", "b"], {"a"}) == 1.0

    def test_third_position(self):
        assert M.mrr(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)

    def test_no_hit(self):
        assert M.mrr(["x"], {"a"}) == 0.0


class TestRouteAnalysis:
    ROUTES = {"keyword": ["a", "b"], "vector": ["c", "a"]}
    REL = {"a", "b"}

    def test_route_recall(self):
        out = M.route_recall(self.ROUTES, self.REL, 2)
        assert out["keyword"] == 1.0
        assert out["vector"] == 0.5

    def test_union_ceiling(self):
        """并集上限：两路合起来能召回 2/2 —— 这是融合策略的理论天花板。"""
        assert M.union_ceiling(self.ROUTES, self.REL) == 1.0

    def test_union_ceiling_is_pool_level_not_topk(self):
        """天花板是**候选池**口径（各路完整候选的并集），不是各路 top-k 的并集。

        旧口径（各路截断到 k）会让天花板**低于**实际 recall，
        得出"融合还能买 -0.06"这种自相矛盾的负数。
        """
        routes = {"keyword": ["a", "b", "c"], "vector": ["d", "e"]}
        # 池里有 a/b/c/d/e：相关集 a/b 全部在池里 → 天花板 1.0
        assert M.union_ceiling(routes, {"a", "b"}) == 1.0
        # 池里没有 z：任何一路都没召回 → 天花板 0
        assert M.union_ceiling(routes, {"z"}) == 0.0
        # 天花板**不随 k 变化**：它是候选池级的标量（相关集只有 a 时就是 1.0）
        assert M.union_ceiling(routes, {"a"}) == 1.0

    def test_ceiling_is_never_below_actual_recall(self):
        """不变量：天花板 ≥ 任何 k 的实际 recall（否则"空间"会是负数）。"""
        routes = {"keyword": ["a", "x"], "vector": ["b", "y"]}
        ceiling = M.union_ceiling(routes, {"a", "b"})
        assert ceiling >= M.recall_at_k(["a", "b"], {"a", "b"}, 2)

    def test_gap_attribution_buckets(self):
        routes = {"keyword": ["a"], "vector": ["z"]}
        fused = ["a", "z"]
        final = ["z"]
        rel = {"a", "b"}
        out = M.gap_attribution(routes, fused, final, rel)
        assert out["fused_not_final"] == 1   # a：召回到、进融合、没进最终
        assert out["not_recalled"] == 1      # b：任何一路都没召回
        assert out["found"] == 0

    def test_gap_attribution_recalled_not_fused(self):
        routes = {"keyword": ["a"], "vector": ["z"]}
        out = M.gap_attribution(routes, fused=["z"], final=["z"], rel={"a"})
        assert out["recalled_not_fused"] == 1
        assert out["not_recalled"] == 0


class TestAggregate:
    def test_macro_average_not_micro(self):
        """宏平均：每题等权。题 A 有 2 个相关块只中 1 个，题 B 有 1 个相关块全中。
        微平均会算成 2/3，宏平均是 (0.5 + 1.0)/2 = 0.75。"""
        results = [
            {"recall": {"5": 0.5}, "ndcg": {"5": 0.5}, "mrr": 0.5},
            {"recall": {"5": 1.0}, "ndcg": {"5": 1.0}, "mrr": 1.0},
        ]
        out = M.aggregate(results)
        assert out["queries"] == 2
        assert out["recall"]["5"] == pytest.approx(0.75)
        assert out["mrr"] == pytest.approx(0.75)

    def test_empty(self):
        assert M.aggregate([])["queries"] == 0


class TestDatasetIO:
    def test_load_queries(self):
        rows = load_queries(EVAL_DIR / "queries.sample.jsonl")
        assert rows and all(r.get("query_id") for r in rows)
        assert {"q001", "q002"} <= {r["query_id"] for r in rows}

    def test_load_qrels(self):
        """标注的第二列是**内容锚点**（与切块方式无关），不是 chunk_id。"""
        qrels = load_qrels(EVAL_DIR / "qrels.sample.tsv")
        assert qrels["q001"]["关键词路用 BM25"] == 2
        assert qrels["q002"]["检索：BM25（关键词路）与向量（语义路）双路召回"] == 0

    def test_load_runs(self):
        runs = load_runs(EVAL_DIR / "runs.sample.jsonl")
        assert "q001" in runs
        assert runs["q001"]["ranked"][0] == "kquery-overview#2"

    def test_runs_roundtrip(self, tmp_path):
        p = tmp_path / "runs.jsonl"
        save_runs(p, {"q1": {"query_id": "q1", "ranked": ["a"]}})
        assert load_runs(p)["q1"]["ranked"] == ["a"]

    def test_missing_file_is_empty_not_error(self, tmp_path):
        assert load_queries(tmp_path / "nope.jsonl") == []
        assert load_qrels(tmp_path / "nope.tsv") == {}


class TestSampleGoldSet:
    """样例集自检：证明评测链路（标注 → 指标 → 归因）能跑通。"""

    def _score(self):
        # 打分只有一份实现（app.services.evaluation.local_eval.score_runs）；
        # 脚本只是 CLI 薄封装——避免"改了脚本忘了改库"导致两边数字对不上。
        from app.services.evaluation.dataset import resolve_anchors
        from app.services.evaluation.local_eval import score_runs
        from app.services.evaluation.policies import chunk_corpus

        runs = load_runs(EVAL_DIR / "runs.sample.jsonl")
        anchors = load_qrels(EVAL_DIR / "qrels.sample.tsv")
        chunks = chunk_corpus(EVAL_DIR / "corpus", "recursive-300-50", "eval-tenant")
        qrels = resolve_anchors(anchors, chunks)
        return score_runs(runs, qrels, (5, 10, 20))

    def test_aggregate_has_headroom(self):
        """样例集里 q006 没召回到答案 → recall@5 = 5/6，而不是虚高的 1.0。"""
        report = self._score()
        assert report["aggregate"]["queries"] == 6
        assert report["aggregate"]["recall"]["5"] == pytest.approx(0.8333, rel=1e-3)

    def test_attribution_reports_missing_chunk(self):
        report = self._score()
        assert report["attribution"]["not_recalled"] == 1
        assert report["attribution"]["found"] == 7

    def test_report_is_json_serialisable(self):
        json.dumps(self._score())


class TestRealGoldSet:
    """真实金标集（20 条，项目自身文档）：格式与标注一致性自检。"""

    def test_queries_and_qrels_present(self):
        queries = load_queries(EVAL_DIR / "queries.jsonl")
        qrels = load_qrels(EVAL_DIR / "qrels.tsv")
        assert len(queries) >= 20
        assert len(qrels) >= 20
        # **标注是查询的子集**，不是相等：`EV-01` 扩容后新增了「否定型」查询
        # （语料里没有 → 期望拒答），它们**故意没有标注**——给它们加标注等于
        # 要求系统「必须召回不存在的块」。
        query_ids = {q["query_id"] for q in queries}
        assert set(qrels) <= query_ids, "标注里有不存在的 query_id"
        unannotated = query_ids - set(qrels)
        for q in queries:
            if q["query_id"] in unannotated:
                assert q.get("question_type") == "negative", (
                    f"{q['query_id']} 没有标注，但它不是否定型查询——"
                    "要么补标注，要么标成 negative"
                )

    def test_every_query_has_an_answer_chunk(self):
        qrels = load_qrels(EVAL_DIR / "qrels.tsv")
        for qid, grades in qrels.items():
            assert grades, f"{qid} 没有标注"
            assert 2 in grades.values(), f"{qid} 缺少 grade=2 的答案块"

    def test_anchors_exist_in_corpus(self):
        """每个锚点都必须真实出现在语料里（抄错短语会让 recall 变成假数字）。"""
        corpus = EVAL_DIR / "corpus"
        text = "\n".join(p.read_text(encoding="utf-8") for p in sorted(corpus.glob("*.md")))
        for qid, anchors in load_qrels(EVAL_DIR / "qrels.tsv").items():
            for anchor in anchors:
                assert anchor in text, f"{qid} 的锚点在语料里不存在：{anchor}"

    def test_anchor_resolution_is_strategy_independent(self):
        """同一份标注在不同切块策略下都要能解析出答案块（否则跨策略比较无从谈起）。"""
        from app.services.evaluation.dataset import resolve_anchors
        from app.services.evaluation.policies import chunk_corpus

        anchor_qrels = load_qrels(EVAL_DIR / "qrels.tsv")
        for spec in ("recursive-300-50", "recursive-800-100"):
            chunks = chunk_corpus(EVAL_DIR / "corpus", spec, "eval-tenant")
            resolved = resolve_anchors(anchor_qrels, chunks)
            missing = sorted(set(anchor_qrels) - set(resolved))
            assert not missing, f"{spec} 下这些标注失效了：{missing}"

class TestGitignoreBoundaries:
    """金标集必须**进版本库**，运行时数据必须**被忽略**。

    这条边界很容易被悄悄破坏：`.gitignore` 里写 `data/` 会匹配任意层级的
    data 目录，连 `tests/data/eval/`（版本化资产）一起被排除——
    表现是"本地能跑、CI 上根本没有这份数据"，而且 git 不会报错。
    """

    @staticmethod
    def _ignored(rel_path: str) -> bool:
        import subprocess

        repo_root = EVAL_DIR.parents[2]  # <root>/tests/data/eval → <root>
        try:
            proc = subprocess.run(
                ["git", "check-ignore", "-q", rel_path],
                cwd=str(repo_root),
                capture_output=True,
            )
        except (OSError, FileNotFoundError):
            return False
        if proc.returncode not in (0, 1):
            return False
        return proc.returncode == 0

    def test_gold_set_is_tracked(self):
        rel = "tests/data/eval/qrels.tsv"
        assert not self._ignored(rel), f"金标集被 .gitignore 忽略了：{rel}"

    def test_runtime_data_is_ignored(self):
        assert self._ignored("data/docstore.json"), "运行时数据必须被 .gitignore 忽略"
