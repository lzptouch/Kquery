"""生成扩展金标集（`EV-01`）：语料文档 + 100 条检索金标 + 50 条问答金标。

## 为什么用脚本生成

手写 100 条查询 + 标注必然出错：锚点短语抄错一个，recall 就变成
「看起来有数字、实际是错的」。脚本按**事实短语**生成标注，
锚点与语料的一致性由代码保证（生成后逐条回查：锚点必须唯一命中）。

## 题型分布（设计文档 §3.1）

| 题型 | 条数 | 测什么 |
|---|---|---|
| `fact` 事实型 | 25 | 单点召回 |
| `multi_hop` 多跳 | 20 | 一问要跨多个事实拼答案 |
| `cross_doc` 跨文档 | 20 | 答案分散在两篇文档 |
| `temporal` 时序 | 15 | 生效日期 / 版本时点 |
| `negative` 否定型 | 10 | **应当拒答**（语料里没有） |
| `contradictory` 矛盾型 | 10 | 两份文件对不上（应指出冲突而不是挑一个） |

用法（仓库根执行）：
    backend/.venv/bin/python scripts/eval/build_goldset_ext.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
EVAL_DIR = REPO_ROOT / "tests" / "data" / "eval"
CORPUS_DIR = EVAL_DIR / "corpus"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(EVAL_DIR))

from corpus_facts import CONTRADICTIONS, CORPUS, NEGATIVE_TOPICS  # noqa: E402

TENANT = "eval-tenant"

#: 事实短语 → 问句的规则（按顺序匹配，第一条命中即用）
QUESTION_RULES: list[tuple[str, str]] = [
    (r"^(.+?)自\s*(\d{4} 年.*?)起执行。$", "{s}什么时候开始执行？"),
    (r"^(.+?)是\s*(.+?)。$", "{s}是什么？"),
    (r"^(.+?)为\s*(.+?)。$", "{s}是多少？"),
    (r"^(.+?)包括\s*(.+?)。$", "{s}包括哪些内容？"),
    (r"^(.+?)采用\s*(.+?)。$", "{s}采用什么口径？"),
    (r"^(.+?)由\s*(.+?)统一\s*(.+?)。$", "{s}由谁负责？"),
    (r"^(.+?)在\s*(.+?)统一\s*(.+?)。$", "{s}什么时候受理？"),
    (r"^(.+?)需在\s*(.+?)之前完成。$", "{s}最晚什么时候完成？"),
    (r"^(.+?)不计入\s*(.+?)。$", "{s}算不算在范围内？"),
    (r"^(.+?)不得\s*(.+?)。$", "{s}允许吗？"),
    (r"^(.+?)需走\s*(.+?)。$", "{s}要走什么流程？"),
    (r"^(.+?)须\s*(.+?)。$", "{s}有什么要求？"),
    (r"^(.+?)需\s*(.+?)。$", "{s}有什么要求？"),
    (r"^(.+?)应\s*(.+?)。$", "{s}有什么要求？"),
]

#: 规则生成不出来的人工覆盖（事实短语 → 问句）
QUESTION_OVERRIDES: dict[str, str] = {}


def question_of(fact: str) -> str:
    """由事实短语生成问句（规则 + 人工覆盖）。"""
    if fact in QUESTION_OVERRIDES:
        return QUESTION_OVERRIDES[fact]
    for pattern, template in QUESTION_RULES:
        m = re.match(pattern, fact)
        if m:
            groups = m.groups()
            subject = groups[0]
            if len(subject) > 22 and "，" in subject:
                subject = subject.rsplit("，", 1)[-1]
            return template.format(s=subject, v=groups[-1])
    return f"关于「{fact[:10]}…」的规定是什么？"


def render_doc(key: str, title: str, domain: str, facts: list[tuple[str, str]]) -> str:
    """把事实渲染成一篇 markdown 语料。

    **每条事实在文档里只出现一次**：标注锚点要求「唯一命中一个块」
    （`services/evaluation/dataset.py` 的 `resolve_anchors` 会把命中多块的锚点
    判为失效）。此前版本把同一句放在「规定明细 / 速查表 / 常见问题」三处，
    结果每条锚点都命中 3 个块 → 全部标注失效。
    """
    lines = [
        f"# {title}",
        "",
        f"> 领域：{domain}　文档编号：{key}",
        "",
        "## 规定明细",
        "",
    ]
    for idx, (fact, kind) in enumerate(facts, start=1):
        tag = "（时效条款）" if kind == "t" else ""
        lines.append(f"{idx}. {fact}{tag}")
    lines += [
        "",
        "## 适用范围",
        "",
        f"本规范适用于与「{title}」相关的全部业务场景；"
        "与其它制度冲突时，以生效日期更晚者为准。",
        "",
    ]
    return "\n".join(lines) + "\n"


def build_corpus() -> None:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    for key, (title, domain, facts) in CORPUS.items():
        (CORPUS_DIR / f"{key}.md").write_text(render_doc(key, title, domain, facts), encoding="utf-8")
    print(f"已生成 {len(CORPUS)} 篇语料文档 → {CORPUS_DIR}")


def fact_index() -> list[tuple[str, str, str, str]]:
    """(doc_key, 标题, 事实短语, 类型) 列表。"""
    out: list[tuple[str, str, str, str]] = []
    for key, (title, _domain, facts) in CORPUS.items():
        for fact, kind in facts:
            out.append((key, title, fact, kind))
    return out


def build_queries() -> tuple[list[dict], dict[str, dict[str, int]]]:
    """生成 100 条查询 + 标注（锚点 = 事实短语）。"""
    facts = fact_index()
    rows: list[dict] = []
    qrels: dict[str, dict[str, int]] = {}
    seq = 0

    def add(question, doc_id, qtype, difficulty, anchors, tags=None):
        nonlocal seq
        seq += 1
        qid = f"e{seq:03d}"
        rows.append({
            "query_id": qid,
            "question": question,
            "doc_id": doc_id,
            "question_type": qtype,
            "difficulty": difficulty,
            "requires_vision": False,
            "tags": tags or [qtype],
        })
        qrels[qid] = {a: 2 for a in anchors}

    # ① 事实型 25 条
    fact_rows = [f for f in facts if f[3] == "f"]
    step = max(1, len(fact_rows) // 25)
    for doc, _title, fact, _kind in fact_rows[::step][:25]:
        add(question_of(fact), doc, "fact", "easy", [fact])

    # ② 多跳 20 条：同一文档内的两条事实
    by_doc: dict[str, list[tuple[str, str, str, str]]] = {}
    for row in facts:
        by_doc.setdefault(row[0], []).append(row)
    multi = 0
    for doc, rows_in_doc in by_doc.items():
        for i in range(0, len(rows_in_doc) - 1, 2):
            if multi >= 20:
                break
            a, b = rows_in_doc[i][2], rows_in_doc[i + 1][2]
            add(f"关于「{a[:-1]}」与「{b[:-1]}」的规定分别是什么？",
                doc, "multi_hop", "medium", [a, b])
            multi += 1
        if multi >= 20:
            break

    # ③ 跨文档 20 条：两篇文档的事实组合
    cross = 0
    doc_keys = sorted(by_doc)
    for i in range(len(doc_keys)):
        for j in range(i + 1, len(doc_keys)):
            if cross >= 20:
                break
            a = by_doc[doc_keys[i]][0][2]
            b = by_doc[doc_keys[j]][0][2]
            add(f"「{a[:-1]}」与「{b[:-1]}」分别是怎么规定的？",
                doc_keys[i], "cross_doc", "medium", [a],
                tags=["cross_doc", doc_keys[j]])
            qrels[rows[-1]["query_id"]][b] = 1
            cross += 1
        if cross >= 20:
            break

    # ④ 时序 15 条：生效日期类事实
    for doc, _title, fact, _kind in [f for f in facts if f[3] == "t"][:15]:
        add(question_of(fact), doc, "temporal", "easy", [fact])

    # ⑤ 否定型 10 条：语料里没有 → 期望拒答（无标注）
    for question, _note in NEGATIVE_TOPICS:
        add(question, "", "negative", "hard", [])

    # ⑥ 矛盾型 10 条：两份文件对不上
    for topic, (doc_a, fact_a), (doc_b, fact_b) in CONTRADICTIONS:
        add(f"{topic}的规定是多少？两份文件似乎不一致，到底按哪个？", doc_a,
            "contradictory", "hard", [fact_a, fact_b],
            tags=["contradictory", doc_b])
    return rows, qrels


def build_answers(rows: list[dict], qrels: dict[str, dict[str, int]]) -> list[dict]:
    """生成问答金标（≥50 条）：事实型 + 多跳 + 跨文档。

    **锚点直接取自 qrels**（而不是重新从问句反推）——标注与金标同源，
    不会出现「检索金标与问答金标指向不同事实」的错位。
    """
    out: list[dict] = []
    for row in rows:
        if len(out) >= 60:
            break
        if row["question_type"] not in ("fact", "multi_hop", "cross_doc"):
            continue
        anchors = [a for a, g in (qrels.get(row["query_id"]) or {}).items() if g >= 2]
        if not anchors:
            continue
        out.append({
            "query_id": row["query_id"],
            "question": row["question"],
            "answer": "；".join(anchors),
            "key_points": anchors,
            "must_not_contain": [],
            "question_type": row["question_type"],
            "source_doc": row["doc_id"],
        })
    return out


def main() -> int:
    from app.services.evaluation.dataset import load_qrels, save_qrels

    build_corpus()
    rows, qrels = build_queries()

    # 合并手写的 20 条（项目自身文档的锚点）——保留原有金标，只扩容。
    # **基准集单独存**（`queries_base.jsonl`）：否则脚本不幂等——
    # 第二次跑会把上次生成的 100 条再合并一遍（220 条 → 320 条）。
    base_path = EVAL_DIR / "queries_base.jsonl"
    original: list[dict] = []
    if base_path.exists():
        for line in base_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                original.append(json.loads(line))
    original_qrels = load_qrels(EVAL_DIR / "qrels_base.tsv")

    merged_rows = original + rows
    merged_qrels = {**original_qrels, **qrels}

    (EVAL_DIR / "queries.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in merged_rows) + "\n",
        encoding="utf-8",
    )
    save_qrels(EVAL_DIR / "qrels.tsv", merged_qrels)

    answers = build_answers(rows, qrels)
    (EVAL_DIR / "answers.jsonl").write_text(
        "\n".join(json.dumps(a, ensure_ascii=False) for a in answers) + "\n",
        encoding="utf-8",
    )

    by_type: dict[str, int] = {}
    for r in merged_rows:
        key = r.get("question_type", "fact")
        by_type[key] = by_type.get(key, 0) + 1
    print(f"查询：{len(merged_rows)} 条（原有 {len(original)} + 新增 {len(rows)}）")
    print(f"题型分布：{by_type}")
    print(f"问答金标：{len(answers)} 条")
    print(f"标注：{len(merged_qrels)} 组")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
