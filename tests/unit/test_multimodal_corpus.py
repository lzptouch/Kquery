"""多模态（图文）语料与金标集的守卫测试。

**为什么第三方素材进仓库必须有守卫测试**：
- 图片文件被截断/损坏 → PIL 打不开 → 图文链路静默失效；
- 有人换了图却没更新 `SOURCES.md` → **许可不明**（合规问题，代码里看不出来）；
- 图注与图片对不上（图注是猜的）→ 评测数字是假的；
- 锚点失效 → 标注被静默丢弃 → 报告里的 recall 看起来很高、实际没评到东西。

这几类问题**都不会让测试变红**，除非专门盯着它们。
"""
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS = REPO_ROOT / "tests" / "data" / "eval" / "corpus_img"
EVAL_DIR = REPO_ROOT / "tests" / "data" / "eval"

IMAGES = sorted((CORPUS / "images").glob("*"))
CAPTIONS = sorted((CORPUS / "captions").glob("*.txt"))
SOURCES = CORPUS / "SOURCES.md"


def _image_stem(path: Path) -> str:
    """图片文件名 → 去掉扩展名的标识（用于与图注一一对应）。"""
    return path.stem


class TestCorpusAssets:
    def test_images_present(self):
        assert len(IMAGES) >= 10, f"图文语料图片太少：{len(IMAGES)}"

    def test_every_image_is_readable(self):
        """图片必须是有效的、能被解码的图像（截断的 PNG 会让图文链路静默失效）。"""
        Image = pytest.importorskip("PIL.Image")
        for path in IMAGES:
            with Image.open(path) as img:
                img.verify()  # verify 能抓出截断的文件
            with Image.open(path) as img:
                width, height = img.size
            assert width > 0 and height > 0, f"{path.name} 尺寸异常"

    def test_images_are_not_too_large(self):
        """单文件 ≤ 600KB：语料进仓库，超过这个量就该换更小的图。"""
        for path in IMAGES:
            size_kb = path.stat().st_size / 1024
            assert size_kb <= 600, f"{path.name} 过大：{size_kb:.0f}KB"

    def test_every_image_has_a_caption(self):
        """图与图注一一对应：没有图注的图在检索里等于不存在。"""
        stems = {_image_stem(p) for p in IMAGES}
        for path in CAPTIONS:
            assert path.stem.replace("img-", "") in stems, f"图注无对应图片：{path.name}"
        assert len(CAPTIONS) == len(IMAGES), (
            f"图注 {len(CAPTIONS)} 份 vs 图片 {len(IMAGES)} 张，数量对不上"
        )

    def test_captions_have_four_sections(self):
        """图注是四段式（主体/属性/场景/图中文字），与生产 caption 的结构一致。"""
        for path in CAPTIONS:
            text = path.read_text(encoding="utf-8")
            for section in ("【主体】", "【属性】", "【场景】", "【图中文字】"):
                assert section in text, f"{path.name} 缺 {section}"


class TestProvenance:
    """第三方素材的出处与许可。"""

    def test_sources_file_exists(self):
        assert SOURCES.exists(), "缺 SOURCES.md：第三方素材进仓库必须登记出处与许可"

    def test_sources_covers_every_image(self):
        text = SOURCES.read_text(encoding="utf-8")
        for path in IMAGES:
            assert path.name in text, f"SOURCES.md 未登记：{path.name}"

    def test_sources_declares_licenses(self):
        text = SOURCES.read_text(encoding="utf-8")
        assert "BSD" in text and "MIT" in text and "PSF" in text

    def test_sources_states_the_third_party_nature(self):
        text = SOURCES.read_text(encoding="utf-8")
        assert "开源" in text and "许可" in text


class TestMultimodalGoldSet:
    def test_queries_and_qrels_present(self):
        queries = [
            json.loads(line)
            for line in (EVAL_DIR / "queries_img.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        qrels = [
            line
            for line in (EVAL_DIR / "qrels_img.tsv")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert len(queries) >= 12
        assert len(qrels) == len(queries)
        assert {q["query_id"] for q in queries} == {line.split("\t")[0] for line in qrels}

    def test_every_query_has_an_image_answer(self):
        """每条图问题都要能落到**某个图注**块上（否则评的是"召回文本"不是"看图"）。"""
        from app.services.evaluation.dataset import resolve_anchors
        from app.services.evaluation.policies import chunk_corpus

        anchors: dict[str, dict[str, int]] = {}
        for line in (
            (EVAL_DIR / "qrels_img.tsv").read_text(encoding="utf-8").splitlines()
        ):
            if not line.strip() or line.startswith("#"):
                continue
            qid, anchor, grade = line.split("\t")
            anchors.setdefault(qid, {})[anchor] = int(grade)

        chunks = chunk_corpus(CORPUS, "recursive-300-50", "eval-tenant")
        resolved = resolve_anchors(anchors, chunks)
        missing = sorted(set(anchors) - set(resolved))
        assert not missing, f"这些标注的锚点没命中任何块：{missing}"

        caption_docs = {c.doc_id for c in chunks if c.doc_id.startswith("img-")}
        assert caption_docs, "语料里没有图注块（captions/ 没被切块）"

    def test_image_questions_are_grounded_in_captions(self):
        """图问题的锚点必须真的出现在**图注文本**里（不能指到 README）。"""
        captions = "\n".join(p.read_text(encoding="utf-8") for p in CAPTIONS)
        for line in (
            (EVAL_DIR / "qrels_img.tsv").read_text(encoding="utf-8").splitlines()
        ):
            if not line.strip() or line.startswith("#"):
                continue
            qid, anchor, _grade = line.split("\t")
            if not qid.startswith("i"):
                continue
            assert anchor in captions, f"{qid} 的锚点不在图注里：{anchor}"


class TestAdversarialVisionSet:
    """对抗题集：**必须看原图**才能答的题（M1/M5 的价值证明）。

    这组题的设计前提：**答案不在图注里**。
    如果答案其实能从图注读出来，那这组题就退化成普通图文题，
    "跨模态缺口"这个数字就是假的——所以这里用**不变量测试**钉死它。
    """

    @staticmethod
    def _queries() -> list[dict]:
        return [
            json.loads(line)
            for line in (EVAL_DIR / "queries_adv.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]

    @staticmethod
    def _qrels() -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for line in (EVAL_DIR / "qrels_adv.tsv").read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            qid, anchor, grade = line.split("\t")
            out.setdefault(qid, {})[anchor] = int(grade)
        return out

    def test_every_query_requires_vision(self):
        queries = self._queries()
        assert len(queries) >= 10
        assert all(q.get("requires_vision") for q in queries)
        assert all(q.get("answer_key") for q in queries)

    def test_answer_key_is_absent_from_the_caption(self):
        """**核心不变量**：答案关键词不能出现在该题的图注里。

        否则这题就是"文本可答"，跨模态缺口会被虚高/虚低。
        """
        from app.services.evaluation.dataset import resolve_anchors
        from app.services.evaluation.policies import chunk_corpus

        anchors = self._qrels()
        chunks = chunk_corpus(CORPUS, "recursive-300-50", "eval-tenant")
        resolved = resolve_anchors(anchors, chunks)
        by_id = {c.chunk_id: c for c in chunks}
        queries = {q["query_id"]: q for q in self._queries()}

        missing = sorted(set(anchors) - set(resolved))
        assert not missing, f"锚点失效：{missing}"

        for qid, grades in resolved.items():
            key = queries[qid]["answer_key"]
            for cid in grades:
                content = by_id[cid].content or ""
                assert key not in content, (
                    f"{qid} 的答案关键词「{key}」出现在图注里 → 这题不是'必须看图'的题："
                    f"{content[:80]}"
                )

    def test_annotations_have_fact_question_and_answer(self):
        """每张图的标注都要有：视觉事实 / 问题 / 期望答案。"""
        files = sorted((CORPUS / "annotations").glob("img-*.md"))
        assert len(files) >= 10
        for path in files:
            text = path.read_text(encoding="utf-8")
            assert "事实：" in text, f"{path.name} 缺视觉事实"
            assert "问题（必须看图才能答）：" in text, f"{path.name} 缺问题"
            assert "期望答案：" in text, f"{path.name} 缺期望答案"

    def test_text_answerability_is_zero(self):
        """跑一遍评测：**文本可答性必须为 0**（这组题都只能靠原图）。"""
        from app.services.evaluation.dataset import resolve_anchors
        from app.services.evaluation.local_eval import build_retriever, run_queries, score_runs
        from app.services.evaluation.policies import chunk_corpus

        chunks = chunk_corpus(CORPUS, "recursive-300-50", "eval-tenant")
        anchors = self._qrels()
        qrels = resolve_anchors(anchors, chunks)
        queries = self._queries()
        retriever = build_retriever(chunks)
        runs = run_queries(retriever, queries, "eval-tenant", top_k=10)
        kinds = {q["query_id"]: "vision" for q in queries}
        answers = {q["query_id"]: q["answer_key"] for q in queries}
        chunk_text = {c.chunk_id: c.content or "" for c in chunks}
        report = score_runs(
            runs, qrels, (5, 10, 20), kinds=kinds, answers=answers, chunk_text=chunk_text
        )
        agg = report["aggregate"]
        assert agg.get("text_answerable_rate") == 0.0, (
            f"文本可答性应为 0（这组题必须看原图），实得 {agg.get('text_answerable_rate')}"
        )
        # 检索前提：正确的图必须能被找到
        assert agg["recall"]["10"] >= 0.9, (
            f"对抗题的检索前提没达标（recall@10={agg['recall']['10']}）→ "
            "连图都找不到，谈不上'看图直答'"
        )


class TestDocsStayInSync:
    """文档里的关键数字**必须**与代码一致（防止文档漂移）。

    为什么值得测：文档数字过期时**不会有人报错**——它只是"看起来对"。
    这里把最容易漂的几个数字（端点数 / 测试规模 / 评测资产）钉住。
    """

    def test_endpoint_count_in_docs_matches_code(self):
        """`doc/analysis/README.md` 与 `03-接口与契约.md` 的端点数 = 代码实际端点数。"""
        import os
        import tempfile

        os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
        os.environ.setdefault("MODEL_PROVIDER", "mock")
        from app.main import create_app

        spec = create_app().openapi()
        actual = sum(
            1
            for ops in spec["paths"].values()
            for method in ops
            if method.upper() in {"GET", "POST", "PUT", "DELETE", "PATCH"}
        )
        # 认证与授权下线（2026-11-12）：87 → 69（移除 18 个认证/授权/知识库端点）
        # 2026-11-13：70 → 71（`C-09` 新增 `GET /graph/build/{task_id}` 状态查询）
        # 2026-11-13：71 → 72（`X-22` 新增 `GET /ask/stream/{trace_id}` SSE 断点续传）
        # 2026-11-14：72 → 69（语音下线移除 `/speech/*` 三端点，`api/speech.py` 整个删除）
        assert actual == 69, f"端点数变了：{actual}（文档与守卫测试都要同步）"

        index = (REPO_ROOT / "doc" / "analysis" / "README.md").read_text(encoding="utf-8")
        assert f"**{actual}**" in index, f"doc/analysis/README.md 的端点数没同步（应为 {actual}）"

    def test_test_scale_in_docs_matches_reality(self):
        """文档里的「后端测试」规模不得落后于实际收集数。

        口径（2026-11-12 改）：不再把数字**硬编码在守卫里**——
        硬编码意味着「每加一个测试文件就得改一次守卫」，改多了守卫就失去意义
        （它会从「文档与现实一致」退化成「文档与某次历史快照一致」）。

        现在的判据：
        - **文件数**：文档写的数必须**等于**实际（少写 = 文档漏了新测试）；
        - **用例数**：文档写的数必须**不低于**基线（新增用例只增不减，
          文档数字落后于现实才算过期）。
        """
        readme = (REPO_ROOT / "doc" / "analysis" / "README.md").read_text(encoding="utf-8")

        cases_match = re.search(r"\*\*(\d+)\*\* 个用例", readme)
        assert cases_match, "doc/analysis/README.md 里找不到用例数（期望格式：**N** 个用例）"
        doc_cases = int(cases_match.group(1))
        assert doc_cases >= 1885, (
            f"doc/analysis/README.md 的用例数已过期：{doc_cases} < 1885"
        )

        files_match = re.search(r"(\d+) 个测试文件", readme)
        assert files_match, "doc/analysis/README.md 里找不到测试文件数（期望格式：N 个测试文件）"
        files = list((REPO_ROOT / "tests").rglob("test_*.py"))
        assert int(files_match.group(1)) == len(files), (
            f"测试文件数变了：{len(files)}（文档要同步）"
        )

    def test_eval_assets_documented(self):
        """评测资产（语料 / 金标 / 图文素材）必须在评测 README 里有登记。"""
        readme = (REPO_ROOT / "tests" / "data" / "eval" / "README.md").read_text(encoding="utf-8")
        assert "corpus_img" in readme, "评测 README 未登记图文语料"
        assert "SOURCES" in readme or "来源" in readme, "评测 README 未说明素材来源"
        assert (CORPUS / "SOURCES.md").exists(), "图文语料缺来源与许可登记"
