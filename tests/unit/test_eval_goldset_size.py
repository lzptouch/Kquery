"""金标集规模与一致性守卫（`EV-01`）。

## 为什么要有

金标集此前只有 **20 条**（低于「方向性判断下限 50 条」的经验值），
而且**没有问答金标**——生成侧完全无法度量。

守卫的作用是**防悄悄回退**：把金标缩回 20 条时，这个测试必须失败。

## 四条断言

1. 检索金标 ≥ **100** 条；问答金标 ≥ **50** 条
2. 题型分布齐全（事实 / 多跳 / 跨文档 / 时序 / 否定 / 矛盾）
3. **每个锚点都能唯一解析到一个块**（解析不了 = 标注失效 = 指标是假的）
4. 语料文档 ≥ **25** 篇
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[2] / "tests" / "data" / "eval"

#: 检索金标下限（方向性判断 ≥50；本项目目标「检测 5 个百分点」→ ≥100）
MIN_RETRIEVAL_GOLD = 100
#: 问答金标下限
MIN_QA_GOLD = 50
#: 语料文档下限
MIN_CORPUS_DOCS = 25
#: 题型分布（每类至少这么多条）
REQUIRED_TYPES = {
    "fact": 25,
    "multi_hop": 20,
    "cross_doc": 20,
    "temporal": 15,
    "negative": 10,
    "contradictory": 10,
}


def _load_jsonl(name: str) -> list[dict]:
    path = EVAL_DIR / name
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_retrieval_gold_size():
    """① 检索金标 ≥ 100 条。"""
    queries = _load_jsonl("queries.jsonl")
    assert len(queries) >= MIN_RETRIEVAL_GOLD, (
        f"检索金标只有 {len(queries)} 条（要求 ≥ {MIN_RETRIEVAL_GOLD}）："
        "样本量决定结论的置信度，缩回去等于让评测报告失去意义"
    )


def test_qa_gold_size():
    """① 问答金标 ≥ 50 条。"""
    answers = _load_jsonl("answers.jsonl")
    assert len(answers) >= MIN_QA_GOLD, (
        f"问答金标只有 {len(answers)} 条（要求 ≥ {MIN_QA_GOLD}）："
        "生成侧没有金标就无从度量答案质量"
    )


def test_question_type_distribution():
    """② 题型分布齐全（每类达到设计文档的条数）。"""
    queries = _load_jsonl("queries.jsonl")
    counts = Counter(q.get("question_type", "fact") for q in queries)
    for qtype, minimum in REQUIRED_TYPES.items():
        assert counts.get(qtype, 0) >= minimum, (
            f"题型 {qtype} 只有 {counts.get(qtype, 0)} 条（要求 ≥ {minimum}）"
        )


def test_corpus_size():
    """④ 语料文档 ≥ 25 篇。"""
    docs = list((EVAL_DIR / "corpus").glob("*.md"))
    assert len(docs) >= MIN_CORPUS_DOCS, (
        f"语料只有 {len(docs)} 篇（要求 ≥ {MIN_CORPUS_DOCS}）"
    )


def test_all_anchors_resolve_uniquely():
    """③ 每个锚点都能**唯一**解析到一个块（解析不了 = 指标是假的）。"""
    from app.services.chunking import build_chunks
    from app.services.evaluation.dataset import load_qrels, resolve_anchors

    chunks = []
    for path in sorted((EVAL_DIR / "corpus").glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        chunks.extend(
            build_chunks(path.stem, [(path.name, text)], "eval-tenant", 300, 50)
        )
    qrels = load_qrels(EVAL_DIR / "qrels.tsv")
    assert qrels, "没有标注数据"
    resolved = resolve_anchors(qrels, chunks)
    missing = sorted(set(qrels) - set(resolved))
    assert not missing, (
        f"以下标注的锚点解析失败（评测时会被丢弃 → 指标失真）：{missing[:10]}"
    )


def test_queries_have_new_fields():
    """① 扩字段：题型 / 难度 / 是否需要看图。"""
    queries = _load_jsonl("queries.jsonl")
    for q in queries:
        for field in ("question_type", "difficulty", "requires_vision"):
            assert field in q, f"{q.get('query_id')} 缺字段 {field}"


def test_negative_queries_have_no_annotations():
    """否定型查询**不应有标注**（语料里没有 → 召回它反而是错的）。"""
    from app.services.evaluation.dataset import load_qrels

    qrels = load_qrels(EVAL_DIR / "qrels.tsv")
    queries = _load_jsonl("queries.jsonl")
    negatives = [q["query_id"] for q in queries if q.get("question_type") == "negative"]
    assert negatives, "缺少否定型查询（用来测「该拒答时别硬答」）"
    for qid in negatives:
        assert qid not in qrels, f"否定型查询 {qid} 不应有标注"


def test_contradictory_queries_have_two_anchors():
    """矛盾型查询要有**两个**锚点（两份文件各一个）。"""
    from app.services.evaluation.dataset import load_qrels

    qrels = load_qrels(EVAL_DIR / "qrels.tsv")
    queries = _load_jsonl("queries.jsonl")
    contra = [q["query_id"] for q in queries if q.get("question_type") == "contradictory"]
    assert contra, "缺少矛盾型查询"
    for qid in contra:
        assert len(qrels.get(qid, {})) >= 2, f"矛盾型查询 {qid} 应有两个锚点"
