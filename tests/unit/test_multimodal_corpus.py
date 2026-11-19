"""多模态（图文）语料与金标集的守卫测试。

**为什么第三方素材进仓库必须有守卫测试**：
- 图片文件被截断/损坏 → PIL 打不开 → 图文链路静默失效；
- 有人换了图却没更新 `SOURCES.md` → **许可不明**（合规问题，代码里看不出来）；
- 图注与图片对不上（图注是猜的）→ 评测数字是假的；
- 锚点失效 → 标注被静默丢弃 → 报告里的 recall 看起来很高、实际没评到东西。

这几类问题**都不会让测试变红**，除非专门盯着它们。
"""
import json
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
