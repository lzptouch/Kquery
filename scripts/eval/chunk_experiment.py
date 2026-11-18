#!/usr/bin/env python3
"""切块策略实验（批次 1 的核心交付）。

回答一个问题：**默认的 chunk_size=300 / overlap=50 是不是最好的？**

做法：对每套策略
1. 用**生产的切块工厂**切语料（实验测的必须是生产路径）；
2. 装配同一套检索栈（真 BM25 + mock 向量 + 真 RRF/重排）跑同一批查询；
3. 用**同一份标注**（内容锚点，与切块方式无关）打分；
4. 记录质量指标 + 成本代理（块数 / embedding token）。

输出：控制台对比表 + `report.json`（可 diff、可进 CI 门禁）。

用法：
    python scripts/eval/chunk_experiment.py \\
        --queries tests/data/eval/queries.jsonl \\
        --qrels tests/data/eval/qrels.tsv \\
        --out tests/data/eval/chunk_experiment.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# 金标集是**测试资产**（要进仓库、要在 CI 复跑），位置：tests/data/eval/
EVAL_DIR = REPO_ROOT / "tests" / "data" / "eval"
DEFAULT_CORPUS = EVAL_DIR / "corpus"
DEFAULT_QUERIES = EVAL_DIR / "queries.jsonl"
DEFAULT_QRELS = EVAL_DIR / "qrels.tsv"
DEFAULT_OUT = EVAL_DIR / "chunk_experiment.json"


def evaluate_policy(
    policy_spec: str,
    corpus: Path,
    queries: list[dict],
    anchor_qrels: dict,
    tenant: str,
    top_k: int,
    k_values,
) -> dict:
    """跑一套策略，返回指标 + 成本代理。"""
    from app.services.evaluation.dataset import resolve_anchors, unresolved_queries
    from app.services.evaluation.local_eval import (
        build_retriever,
        run_queries,
        score_runs,
    )
    from app.services.evaluation.policies import chunk_corpus, resolve_policy

    chunks = chunk_corpus(corpus, policy_spec, tenant)
    qrels = resolve_anchors(anchor_qrels, chunks)
    missing = unresolved_queries(anchor_qrels, qrels)
    retriever = build_retriever(chunks)
    runs = run_queries(retriever, queries, tenant, top_k=top_k)
    report = score_runs(runs, qrels, k_values=k_values)
    agg = report["aggregate"]
    total_chars = sum(len(c.content or "") for c in chunks)
    return {
        "policy": policy_spec,
        "resolved": resolve_policy(policy_spec)["name"],
        "chunks": len(chunks),
        "chars": total_chars,
        # 成本代理：块数越多 → embedding 调用与向量存储越多（按字符/token 粗估）
        "embed_tokens_est": total_chars // 2,
        "recall": agg.get("recall", {}),
        "ndcg": agg.get("ndcg", {}),
        # `EV-06`：补齐检索指标（precision / hit_rate）
        "precision": agg.get("precision", {}),
        "hit_rate": agg.get("hit_rate", {}),
        "mrr": agg.get("mrr"),
        "route_recall": agg.get("route_recall", {}),
        "union_ceiling": agg.get("union_ceiling"),
        "attribution": report.get("attribution", {}),
        "unresolved_queries": missing,
        # 逐题 nDCG@10（`EV-04` 配对 bootstrap 需要**逐条**值，不能只有聚合）
        "per_query_ndcg10": {
            qid: float((row.get("ndcg") or {}).get("10") or 0.0)
            for qid, row in (report.get("per_query") or {}).items()
        },
    }


def print_table(rows: list[dict]) -> None:
    print("\n=== 切块策略对比（真 BM25 + mock 向量 + 真 RRF/重排；同一份标注）===")
    header = f"{'策略':<26}{'块数':>7}{'recall@5':>10}{'recall@10':>11}{'nDCG@10':>10}{'MRR':>8}{'上限@10':>9}"
    print(header)
    print("-" * len(header))
    for row in rows:
        r = row.get("recall", {})
        n = row.get("ndcg", {})
        ceil = row.get("union_ceiling")
        print(
            f"{row['resolved']:<26}{row['chunks']:>7}"
            f"{r.get('5', '-'):>10}{r.get('10', '-'):>11}{n.get('10', '-'):>10}"
            f"{row.get('mrr', '-'):>8}{ceil if ceil is not None else '-':>9}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="切块策略实验")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--qrels", default=str(DEFAULT_QRELS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--tenant", default="eval-tenant")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--k", default="5,10,20")
    parser.add_argument(
        "--policies",
        default="",
        help="逗号分隔的策略预设名（默认全部预设，见 app/services/evaluation/policies.py）",
    )
    args = parser.parse_args()

    from app.services.evaluation.dataset import load_qrels, load_queries
    from app.services.evaluation.policies import PRESETS, resolve_policy

    k_values = tuple(int(x) for x in args.k.split(",") if x.strip())
    queries = load_queries(args.queries)
    if not queries:
        raise SystemExit(f"[eval] 查询集为空：{args.queries}")
    anchor_qrels = load_qrels(args.qrels)
    if not anchor_qrels:
        raise SystemExit(f"[eval] 标注为空：{args.qrels}")

    specs = [s.strip() for s in args.policies.split(",") if s.strip()] or sorted(PRESETS)
    rows: list[dict] = []
    for spec in specs:
        try:
            resolve_policy(spec)
        except ValueError as exc:
            raise SystemExit(f"[eval] {exc}")
        print(f"[eval] 跑策略 {spec} ...", flush=True)
        rows.append(
            evaluate_policy(
                spec, Path(args.corpus), queries, anchor_qrels,
                args.tenant, args.top_k, k_values,
            )
        )

    print_table(rows)

    # 结论：质量最好（nDCG@10）且块数最少的策略
    best = max(rows, key=lambda r: (float(r.get("ndcg", {}).get("10", 0) or 0), -r["chunks"]))

    # `EV-04`：最优策略的判定从「nDCG 最大」升级为
    # 「**nDCG 最高且与其他策略的差异显著**」——
    # 差异不显著的策略只是「与最优无显著差异」，不能当成「更差」。
    significance: dict = {}
    try:
        from app.services.evaluation.significance import as_dict, paired_bootstrap

        best_scores = best.get("per_query_ndcg10") or {}
        for row in rows:
            if row is best:
                continue
            other = row.get("per_query_ndcg10") or {}
            common = sorted(set(best_scores) & set(other))
            if not common:
                continue
            sig = paired_bootstrap(
                [best_scores[q] for q in common], [other[q] for q in common]
            )
            significance[f"{best['resolved']}_vs_{row['resolved']}"] = as_dict(sig)
    except Exception as exc:  # noqa: BLE001 - 显著性是附加维度，失败不该毁掉整场实验
        significance = {"error": f"显著性计算失败（忽略）：{exc}"}
    default_row = next((r for r in rows if r["resolved"] == "recursive-300-50"), None)
    print(f"\n最佳（nDCG@10 优先，块数次之）：{best['resolved']}")
    if default_row and best["resolved"] != default_row["resolved"]:
        d = float(best["ndcg"].get("10", 0) or 0) - float(default_row["ndcg"].get("10", 0) or 0)
        print(
            f"相对默认 recursive-300-50：nDCG@10 {default_row['ndcg'].get('10')} → "
            f"{best['ndcg'].get('10')}（{d:+.4f}），块数 {default_row['chunks']} → {best['chunks']}"
        )
    else:
        print("默认策略（recursive-300-50）就是当前最优。")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"rows": rows, "best": best["resolved"], "significance": significance},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[eval] 实验报告已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
