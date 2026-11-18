"""`services/generation/`（`B0 X-27` 第一步抽出的纯函数）的测试。

**为什么值得测**：这些函数是「提示词长什么样」与「证据怎么装配」的唯一实现，
抽出来之后既没有 `RagPipeline` 兜底、也没有集成测试盯着每个分支——
一旦渲染少一个 `---` 或去重键写错，表现是「答案引用错乱 / 同一篇文档进两次」，
都不会报错。所以在这里用假候选逐条钉死。
"""

from app.domain.schemas import Chunk, Evidence, RetrievalStats
from app.services.retrieval.base import FusedCandidate
from app.services.retrieval.hybrid import HybridResult


def _chunk(cid: str, *, doc_id: str = "doc1", content: str = "正文", **kw) -> Chunk:
    return Chunk(
        chunk_id=cid,
        doc_id=doc_id,
        seq=1,
        content=content,
        source=kw.pop("source", "第1页"),
        tenant_id="t1",
        **kw,
    )


def _hybrid(*cands: FusedCandidate, reranked: bool = True, rejected=()) -> HybridResult:
    return HybridResult(
        candidates=list(cands),
        stats=RetrievalStats(),
        reranked=reranked,
        rejected=list(rejected),
    )


def _cand(chunk: Chunk, *, rrf: float = 0.5, rerank: float | None = 0.9, routes=("vector",)):
    return FusedCandidate(chunk=chunk, rrf_score=rrf, rerank_score=rerank, routes=list(routes))


class TestEvidenceBlock:
    def test_unused_evidence_is_skipped(self):
        from app.services.generation.prompt import build_evidence_block

        block = build_evidence_block(
            [
                Evidence(rank=1, chunk_id="c1", score=0.9, content="用到的", source="a · 第1页",
                         doc_id="doc1", filename="a", used=True),
                Evidence(rank=2, chunk_id="c2", score=0.1, content="没用到", source="a · 第2页",
                         doc_id="doc1", filename="a", used=False),
            ]
        )
        assert "用到的" in block
        assert "没用到" not in block

    def test_each_item_has_paired_boundary_markers(self):
        from app.services.generation.prompt import (
            EVIDENCE_BLOCK_CLOSE,
            EVIDENCE_BLOCK_OPEN,
            build_evidence_block,
        )

        block = build_evidence_block(
            [Evidence(rank=1, chunk_id="c1", score=0.9, content="正文", source="a · 第1页",
                      doc_id="doc1", filename="a", used=True)]
        )
        assert block.count(EVIDENCE_BLOCK_OPEN) == 1
        assert block.count(EVIDENCE_BLOCK_CLOSE) == 1
        # `---` 必须在内容之后、闭合标记之前：MockLLM 的证据正则以 `\n---` 终止上一条
        assert block.index("正文") < block.index("\n---\n") < block.index(EVIDENCE_BLOCK_CLOSE)
        assert "[#1]（来源：a · 第1页）" in block

    def test_doc_boundary_separator(self):
        """跨文档时插 `---` 分隔（同一文档内不插）。"""
        from app.services.generation.prompt import build_evidence_block

        ev = [
            Evidence(rank=1, chunk_id="c1", score=0.9, content="甲", source="a", doc_id="doc1",
                     filename="a", used=True),
            Evidence(rank=2, chunk_id="c2", score=0.8, content="乙", source="a", doc_id="doc1",
                     filename="a", used=True),
            Evidence(rank=3, chunk_id="c3", score=0.7, content="丙", source="b", doc_id="doc2",
                     filename="b", used=True),
        ]
        block = build_evidence_block(ev)
        # 3 条证据各自收尾一个 `---`，再加 doc1→doc2 之间的文档分隔 `---` = 4
        assert block.count("\n---\n") == 4

    def test_empty_evidence_placeholder(self):
        from app.services.generation.prompt import build_evidence_block

        assert build_evidence_block([]) == "（无证据）"

    def test_prompt_contains_no_info_and_question(self):
        from app.domain.constants import NO_INFO_ANSWER
        from app.services.generation.prompt import build_prompt

        prompt = build_prompt("怎么请假？", [])
        assert "怎么请假？" in prompt
        assert NO_INFO_ANSWER in prompt
        assert "（无证据）" in prompt

    def test_pipeline_reexports_are_the_same_objects(self):
        """搬家不改导入路径：`services.pipeline` 里仍是同一份实现。"""
        import app.services.generation.prompt as gen
        import app.services.pipeline as pipeline

        assert pipeline.build_evidence_block is gen.build_evidence_block
        assert pipeline.PROMPT_TEMPLATE is gen.PROMPT_TEMPLATE
        assert pipeline.GENERAL_KNOWLEDGE_PROMPT is gen.GENERAL_KNOWLEDGE_PROMPT
        assert pipeline.REASON_NO_CONTENT == "no_content"


class TestAssetUri:
    def test_page_asset(self):
        from app.services.generation.evidence import asset_uri

        assert asset_uri(_chunk("d#1", doc_id="d", related_assets=["d#page-2"])) == "d#page-2.png"

    def test_img_asset(self):
        from app.services.generation.evidence import asset_uri

        assert asset_uri(_chunk("d#1", doc_id="d", related_assets=["d#img-3"])) == "d-img3"

    def test_no_assets(self):
        from app.services.generation.evidence import asset_uri

        assert asset_uri(_chunk("d#1", related_assets=[])) == ""

    def test_pipeline_still_exports_asset_uri(self):
        from app.services.generation.evidence import asset_uri
        from app.services.pipeline import _asset_uri

        assert _asset_uri is asset_uri


class TestAvgScore:
    def test_top_n_mean(self):
        from app.services.generation.evidence import avg_score

        ev = [
            Evidence(rank=1, chunk_id="c1", score=0.9, content="x", source="s", doc_id="d1",
                     filename="d1", used=True),
            Evidence(rank=2, chunk_id="c2", score=0.7, content="x", source="s", doc_id="d1",
                     filename="d1", used=True),
            Evidence(rank=3, chunk_id="c3", score=0.1, content="x", source="s", doc_id="d1",
                     filename="d1", used=True),
        ]
        avg, n = avg_score(ev, 2)
        assert n == 2
        assert abs(avg - 0.8) < 1e-9

    def test_none_scores_ignored(self):
        from app.services.generation.evidence import avg_score

        ev = [
            Evidence(rank=1, chunk_id="c1", score=None, content="x", source="s", doc_id="d1",
                     filename="d1", used=True),
            Evidence(rank=2, chunk_id="c2", score=0.4, content="x", source="s", doc_id="d1",
                     filename="d1", used=True),
        ]
        assert avg_score(ev, 5) == (0.4, 1)

    def test_empty(self):
        from app.services.generation.evidence import avg_score

        assert avg_score([], 3) == (0.0, 0)


class TestBuildCandidates:
    def test_score_comes_from_rerank_when_reranked(self):
        from app.services.generation.evidence import build_candidates

        candidates, meta = build_candidates(
            _hybrid(_cand(_chunk("c1"), rrf=0.2, rerank=0.88)),
            name_of=lambda _doc: "文件甲",
        )
        assert candidates[0].score == 0.88
        assert candidates[0].source == "文件甲 · 第1页"
        assert meta["c1"][2] == "文件甲 · 第1页"

    def test_score_falls_back_to_rrf_when_degraded(self):
        from app.services.generation.evidence import build_candidates

        candidates, _ = build_candidates(
            _hybrid(_cand(_chunk("c1"), rrf=0.2, rerank=0.88), reranked=False),
            name_of=lambda _doc: "f",
        )
        assert candidates[0].score == 0.2

    def test_none_score_becomes_zero(self):
        from app.services.generation.evidence import build_candidates

        candidates, _ = build_candidates(
            _hybrid(_cand(_chunk("c1"), rerank=None), reranked=True),
            name_of=lambda _doc: "f",
        )
        assert candidates[0].score == 0.0

    def test_parent_expansion_uses_parent_content_and_dedupes(self):
        """父子块：命中两个子块 → 只留一条（父块全文），键是 `parent:<id>`。"""
        from app.services.generation.evidence import build_candidates

        parent = _chunk("p1", content="父块全文")

        def parent_of(pid: str):
            return parent if pid == "p1" else None

        cands = [
            _cand(_chunk("c1", parent_id="p1"), rerank=0.9),
            _cand(_chunk("c2", parent_id="p1"), rerank=0.8),
        ]
        candidates, meta = build_candidates(
            _hybrid(*cands), name_of=lambda _doc: "f", parent_of=parent_of
        )
        assert len(candidates) == 1
        assert candidates[0].key == "parent:p1"
        assert candidates[0].content == "父块全文"
        assert candidates[0].score == 0.9  # 保留先出现（分数更高）的那次
        assert "parent:p1" in meta

    def test_no_parent_of_means_no_expansion(self):
        """没有文档存储时 `parent_of=None`：子块原样进上下文，不静默丢内容。"""
        from app.services.generation.evidence import build_candidates

        candidates, _ = build_candidates(
            _hybrid(_cand(_chunk("c1", parent_id="p1", content="子块")), ),
            name_of=lambda _doc: "f",
            parent_of=None,
        )
        assert len(candidates) == 1
        assert candidates[0].content == "子块"

    def test_missing_parent_falls_back_to_child(self):
        from app.services.generation.evidence import build_candidates

        candidates, _ = build_candidates(
            _hybrid(_cand(_chunk("c1", parent_id="p1", content="子块"))),
            name_of=lambda _doc: "f",
            parent_of=lambda _pid: None,
        )
        assert candidates[0].content == "子块"
        assert candidates[0].key == "c1"

    def test_rank_is_retrieval_order(self):
        from app.services.generation.evidence import build_candidates

        candidates, _ = build_candidates(
            _hybrid(
                _cand(_chunk("c1"), rerank=0.9),
                _cand(_chunk("c2"), rerank=0.8),
            ),
            name_of=lambda _doc: "f",
        )
        assert [c.rank for c in candidates] == [1, 2]


class TestEvidenceAssembly:
    def test_assembled_and_rejected_ranked_continuously(self):
        from app.services.context import assemble_context
        from app.services.generation.evidence import (
            build_candidates,
            evidence_for_assembled,
            rejected_evidence,
        )

        rejected_cand = _cand(_chunk("r1", content="未采纳"), rerank=0.01)
        hybrid = _hybrid(
            _cand(_chunk("c1"), rerank=0.9),
            _cand(_chunk("c2"), rerank=0.8),
            rejected=[rejected_cand],
        )
        candidates, meta = build_candidates(hybrid, name_of=lambda _doc: "f")
        assembled = assemble_context(candidates, token_budget=10000, mmr_lambda=1.0)

        used = evidence_for_assembled(assembled, meta, name_of=lambda _doc: "f")
        assert all(e.used for e in used)
        assert [e.rank for e in used] == [1, 2]

        unused = rejected_evidence(hybrid, name_of=lambda _doc: "f", start_rank=len(used) + 1)
        assert [e.used for e in unused] == [False]
        assert unused[0].rank == 3
        assert unused[0].rejected_reason

    def test_related_assets_survive_assembly(self):
        from app.services.context import assemble_context
        from app.services.generation.evidence import build_candidates, evidence_for_assembled

        hybrid = _hybrid(_cand(_chunk("c1", doc_id="d", related_assets=["d#img-1"]), rerank=0.9))
        candidates, meta = build_candidates(hybrid, name_of=lambda _doc: "f")
        assembled = assemble_context(candidates, token_budget=10000, mmr_lambda=1.0)
        used = evidence_for_assembled(assembled, meta, name_of=lambda _doc: "f")
        assert used[0].related_assets == ["d#img-1"]
        assert used[0].uri == "d-img1"
