"""生成金标集（queries.jsonl + qrels.tsv）。

**为什么用脚本而不是手写**：手写 qrels 会把 chunk_id 抄错，而 chunk_id 依赖
切块边界——一个抄错的 id 会让 recall 变成"看起来有数字、实际是错的"。
脚本按"答案所在段落的关键短语"定位块，标注与 chunk_id 的一致性由代码保证。

用法（在仓库根执行）：
    backend/.venv/bin/python scripts/eval/build_goldset.py
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# 金标集是**测试资产**：tests/data/eval/
EVAL_DIR = REPO_ROOT / "tests" / "data" / "eval"
CORPUS = EVAL_DIR / "corpus"
TENANT = "eval-tenant"

# (query_id, 问题, 语料文件, 答案所在段落的判定短语, [grade2 的短语], [(grade1 短语, ...)])
# grade2 = 该块独立足以回答；grade1 = 相关但不足以独立回答。
#: (query_id, 问题, 语料文件, 答案锚点短语, [grade=1 的相关块锚点])
#: 锚点 = 答案所在段落的一句独特文本。**与切块方式无关**，
#: 所以换一套 chunk 参数/策略后，同一份标注依然有效（评测时按锚点定位块）。
SPEC: list[tuple[str, str, str, str, list[str]]] = [
    ("q01", "BM25 打分用的是哪个公式？IDF 为什么取 ln(1+…)？",
     "05-检索链路", "共享任一查询词的文档得分必然 > 0", ["中文分词用 **jieba**"]),
    ("q02", "为什么 BM25 要「先过滤再排序」，而不是排完再跳过不合格的块？",
     "05-检索链路", "显式过滤把 ACL 判定收敛到一处", []),
    ("q03", "零分文档为什么不进入 RRF 排名？",
     "05-检索链路", "否则它们会占据 rank 位次", []),
    ("q04", "Milvus 的全量查询为什么要分页拉取？不分页会怎样？",
     "05-检索链路", "超过 16k 块后静默只取前 16k", []),
    ("q05", "FAISS 侧的「迭代扩取」解决什么问题？什么时候会告警？",
     "05-检索链路", "可能因过滤导致召回不足", []),
    ("q06", "RRF 融合为什么只用排名、不用分数？",
     "05-检索链路", "校准参数一旦随语料漂移", ["权重按基础路名查找"]),
    ("q07", "索引版本过滤为什么放在融合前，而不是让各后端各自过滤？",
     "05-检索链路", "统一在编排层过滤", []),
    ("q08", "重排用的是哪个模型？是本地部署还是托管服务？",
     "05-检索链路", "只走接口不部署", []),
    ("q09", "令牌桶的回填为什么不允许多于容量？",
     "11-并发与容量", "回填不超过容量", ["参数取值优先级：运行时热配置"]),
    ("q10", "有界执行器解决了哪三个问题？",
     "11-并发与容量", "无界排队导致请求排队到死", []),
    ("q11", "single-flight 的等待上限是多久？超时后怎么办？",
     "11-并发与容量", "等待上限 `_SF_WAIT_S=30s`，超时自行执行", []),
    ("q12", "上游隔离仓的熔断参数是什么？半开要连续成功几次？",
     "11-并发与容量", "window=20 / fail_ratio=0.5 / open_s=30 / half_open=3", ["AIMD 自适应并发"]),
    ("q13", "按 Little's Law 算，目标 4 QPS、平均耗时 8 秒需要多少在飞请求？",
     "11-并发与容量", "例：目标 4 QPS、L=8s → 32 在飞", ["校验硬约束"]),
    ("q14", "检索召回池为什么不能复用问答执行器？",
     "11-并发与容量", "线程等自己池", []),
    ("q15", "多副本部署下哪些状态仍然不共享？",
     "11-并发与容量", "仍是进程内或本地文件", []),
    ("q16", "Prometheus 指标的 label 为什么用 path 而不是 tenant？",
     "10-可观测性与成本", "tenant 随客户数线性增长", []),
    ("q17", "指标里会不会存用户的查询原文？隐私是怎么处理的？",
     "10-可观测性与成本", "防止调用方误以为还能拿到原文", []),
    ("q18", "成本账本里 priced 与 estimated 两个标记分别表示什么？",
     "10-可观测性与成本", "宁可显示「未知」也不给错误数字做预算依据", []),
    ("q19", "探针分几级？/livez 为什么不查依赖？",
     "10-可观测性与成本", "依赖抖动会误杀 Pod", ["把认证档位与共享状态后端暴露出来"]),
    ("q20", "core/ 层允许依赖什么？单向依赖带来什么实际好处？",
     "02-系统架构与运行时", "可以被单独导入而不触发任何服务装配", ["不得依赖 `api/`"]),
]


def build_chunks_by_doc() -> dict[str, list]:
    from app.services.chunking import build_chunks

    out: dict[str, list] = {}
    for path in sorted(CORPUS.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        out[path.stem] = build_chunks(
            path.stem, [(path.name, text)], TENANT, 300, 50
        )
    return out


def find_chunk(chunks: list, phrase: str) -> str | None:
    """按短语定位 chunk_id（**用于自检**：锚点必须唯一命中，否则拒绝标注）。"""
    hits = [c.chunk_id for c in chunks if phrase in c.content]
    if len(hits) != 1:
        return None
    return hits[0]


def main() -> int:
    from app.services.evaluation.dataset import save_qrels

    by_doc = build_chunks_by_doc()
    rows: list[dict] = []
    qrels: dict[str, dict[str, int]] = {}
    problems: list[str] = []
    for qid, question, doc, phrase, extra in SPEC:
        chunks = by_doc.get(doc) or []
        cid = find_chunk(chunks, phrase)
        if cid is None:
            hits = sum(1 for c in chunks if phrase in c.content)
            problems.append(f"{qid}: 短语 {phrase!r} 在 {doc} 命中 {hits} 块（要求恰好 1 块）")
            continue
        # 标注存**锚点**（不是 chunk_id）：换切块策略后依然有效
        qrels[qid] = {phrase: 2}
        for e in extra:
            ecid = find_chunk(chunks, e)
            if ecid and ecid != cid:
                # grade=1 的锚点不能与 grade=2 的锚点相同
                qrels[qid][e] = 1
        rows.append({"query_id": qid, "question": question, "doc_id": doc})

    if problems:
        print("标注生成失败：")
        for p in problems:
            print("  -", p)
        return 1

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    queries_path = EVAL_DIR / "queries.jsonl"
    queries_path.write_text(
        "\n".join(
            __import__("json").dumps(r, ensure_ascii=False) for r in rows
        ) + "\n",
        encoding="utf-8",
    )
    save_qrels(EVAL_DIR / "qrels.tsv", qrels)
    print(f"已生成 {len(rows)} 条查询与 {len(qrels)} 组标注")
    for qid, question, *_ in SPEC:
        print(f"  {qid}: {question} -> {list(qrels.get(qid, {}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
