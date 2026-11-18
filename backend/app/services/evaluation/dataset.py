"""金标集 / 检索结果的读写（纯 stdlib，零依赖）。

数据格式（都是**行分隔文本**，便于 diff 与 code review）：

``queries.jsonl``
    每行一个 JSON：``{"query_id": "q001", "question": "...", "doc_id": "...",
    "tags": ["table", "multi_hop"]}``

``qrels.tsv``
    每行 ``query_id<TAB>anchor<TAB>grade``（grade ∈ {0,1,2}）

    **第二列是内容锚点（anchor），不是 chunk_id**。原因：chunk_id 依赖切块边界，
    换一套切块参数（chunk_size / overlap / 策略）整份标注就失效了——
    而"切块策略哪个更好"恰恰是我们要用这套标注回答的问题。
    锚点是答案所在段落的一句独特文本，**与切块方式无关**：
    评测时按锚点在当前切块结果里定位它落在哪个块（见 `resolve_anchors`）。

``runs.jsonl``
    检索结果（评测的输入）：每行
    ``{"query_id": "q001", "ranked": ["chunk#1", ...],
    "routes": {"keyword": [...], "vector": [...]},
    "fused": [...]}``
    ——``routes``/``fused`` 可选；给了才能做分路/并集/归因分析。
"""
from __future__ import annotations

import json
from pathlib import Path


def load_queries(path: str | Path) -> list[dict]:
    """读取查询集（跳过空行与注释行）。"""
    out: list[dict] = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("query_id"):
            out.append(row)
    return out


def load_qrels(path: str | Path) -> dict[str, dict[str, int]]:
    """读取标注：``{query_id: {anchor: grade}}``（第二列是内容锚点，见模块说明）。"""
    out: dict[str, dict[str, int]] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            parts = line.split()
        if len(parts) < 3:
            continue
        qid, cid, grade = parts[0], parts[1], parts[2]
        try:
            g = int(grade)
        except ValueError:
            continue
        out.setdefault(qid, {})[cid] = g
    return out


def save_qrels(path: str | Path, qrels: dict[str, dict[str, int]]) -> None:
    """写回标注（按 query_id 排序，保证 diff 稳定）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for qid in sorted(qrels):
        for cid, g in sorted(qrels[qid].items()):
            lines.append(f"{qid}\t{cid}\t{int(g)}")
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def load_runs(path: str | Path) -> dict[str, dict]:
    """读取检索结果：``{query_id: {"ranked": [...], "routes": {...}, "fused": [...]}}``。"""
    out: dict[str, dict] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        qid = row.get("query_id")
        if qid:
            out[qid] = row
    return out


def save_runs(path: str | Path, runs: dict[str, dict]) -> None:
    """写回检索结果（按 query_id 排序）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(runs[qid], ensure_ascii=False, sort_keys=True) for qid in sorted(runs)
    ]
    p.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def resolve_anchors(
    anchor_grades: dict[str, dict[str, int]],
    chunks: list,
) -> dict[str, dict[str, int]]:
    """把内容锚点解析成**当前切块下的 chunk_id**。

    一个锚点可能落在多个块里（切块重叠区、或同句在文中重复）：
    取**最短**的那个块——最贴近"这一块独立足以回答"的语义，且结果确定
    （同长度时按 chunk_id 排序，不依赖文件系统顺序）。

    锚点找不到（文档被改写/删除）→ 该标注**整条丢弃**而不是记 0：
    记 0 会把"标注失效"伪装成"召回失败"，那会让指标失真。
    """
    resolved: dict[str, dict[str, int]] = {}
    for qid, anchors in (anchor_grades or {}).items():
        hits: dict[str, int] = {}
        for anchor, grade in (anchors or {}).items():
            matched = [c for c in chunks if anchor and anchor in (c.content or "")]
            if not matched:
                continue
            best = min(matched, key=lambda c: (len(c.content or ""), c.chunk_id))
            # 同一块被多个锚点命中时取**最高** grade（更严格的判定优先）
            hits[best.chunk_id] = max(hits.get(best.chunk_id, 0), int(grade))
        if hits:
            resolved[qid] = hits
    return resolved


def unresolved_queries(
    anchor_grades: dict[str, dict[str, int]],
    resolved: dict[str, dict[str, int]],
) -> list[str]:
    """锚点没落到任何块的查询（标注失效，需要人工复核）。"""
    return sorted(qid for qid in (anchor_grades or {}) if qid not in (resolved or {}))
