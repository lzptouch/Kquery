#!/usr/bin/env python3
"""成本-质量曲线（`EV-06`）：把「质量」与「成本」放在一张表上做帕累托取舍。

## 为什么要有

切块实验此前只报**质量**（recall / nDCG）与一个成本代理（估算 embedding token），
但两者**从没放在一张表上比过**——于是「更大的块 = 更慢更贵但更准」这件事
只能靠感觉权衡。

## 产出

1. `tests/data/eval/cost_quality.json`：每套策略的（质量, 成本）点；
2. **帕累托前沿**（非劣解集合）：不存在另一套策略既更便宜**又**更准。

## 成本口径（全部是**代理量**，不是真实账单）

| 维度 | 口径 |
|---|---|
| 块数 | 切块后的块数（越多 → 向量存储与 embedding 调用越多） |
| embedding token 估算 | 字符数 / 2 |
| 重排调用数 | 每次查询一次重排（与块数无关，与查询数有关） |

**真实账单另算**（`services/agent/cost.py` 的账本）——本脚本的数用于
**相对比较**（哪套策略更划算），不用于报价。

用法（仓库根执行）::

    backend/.venv/bin/python scripts/eval/cost_quality.py \\
        --policies recursive-300-50,recursive-800-100
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# `scripts` 不是包（没有 `__init__.py`）→ 用文件路径直接加载 `chunk_experiment.py`
# 的 `evaluate_policy`，避免为复用一个函数而给 `scripts` 加包结构。
_spec = importlib.util.spec_from_file_location(
    "chunk_experiment_script", REPO_ROOT / "scripts" / "eval" / "chunk_experiment.py"
)
_chunk_experiment = importlib.util.module_from_spec(_spec)
sys.modules["chunk_experiment_script"] = _chunk_experiment
_spec.loader.exec_module(_chunk_experiment)

EVAL_DIR = REPO_ROOT / "tests" / "data" / "eval"
DEFAULT_CORPUS = EVAL_DIR / "corpus"
DEFAULT_QUERIES = EVAL_DIR / "queries.jsonl"
DEFAULT_QRELS = EVAL_DIR / "qrels.tsv"
DEFAULT_OUT = EVAL_DIR / "cost_quality.json"


def pareto_front(points: list[dict]) -> list[dict]:
    """帕累托前沿：不存在另一套策略**既更便宜又更准**（严格更优）。

    `cost` 越小越好、`ndcg10` 越大越好。
    """
    front: list[dict] = []
    for p in points:
        dominated = any(
            (o["cost"] <= p["cost"] and o["ndcg10"] >= p["ndcg10"])
            and (o["cost"] < p["cost"] or o["ndcg10"] > p["ndcg10"])
            for o in points
            if o is not p
        )
        if not dominated:
            front.append(p)
    return front


def main() -> int:
    parser = argparse.ArgumentParser(description="成本-质量曲线（EV-06）")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--qrels", default=str(DEFAULT_QRELS))
    parser.add_argument("--tenant", default="eval-tenant")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--k", default="5,10,20")
    parser.add_argument(
        "--policies", default="recursive-300-50,recursive-800-100",
        help="逗号分隔的切块策略",
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    from app.services.evaluation.dataset import load_qrels, load_queries
    queries = load_queries(args.queries)
    anchor_qrels = load_qrels(args.qrels)
    k_values = tuple(int(x) for x in args.k.split(",") if x.strip())
    policies = [p.strip() for p in args.policies.split(",") if p.strip()]

    points: list[dict] = []
    for policy in policies:
        row = _chunk_experiment.evaluate_policy(
            policy, Path(args.corpus), queries, anchor_qrels, args.tenant,
            args.top_k, k_values,
        )
        ndcg10 = float((row.get("ndcg") or {}).get("10", 0) or 0)
        recall10 = float((row.get("recall") or {}).get("10", 0) or 0)
        precision10 = float((row.get("precision") or {}).get("10", 0) or 0)
        hit_rate10 = float((row.get("hit_rate") or {}).get("10", 0) or 0)
        chunks = int(row.get("chunks", 0) or 0)
        embed_tokens = int(row.get("embed_tokens_est", 0) or 0)
        # 成本代理：embedding token 为主 + 块数（存储 / 检索扫描）的小权重
        cost = embed_tokens + chunks * 100
        points.append({
            "policy": row.get("resolved", policy),
            "chunks": chunks,
            "embed_tokens_est": embed_tokens,
            "cost": cost,
            "recall10": recall10,
            "ndcg10": ndcg10,
            "precision10": precision10,
            "hit_rate10": hit_rate10,
        })
        print(
            f"  {points[-1]['policy']:<24} 成本代理={cost:<8} "
            f"recall@10={recall10:.4f} nDCG@10={ndcg10:.4f} "
            f"precision@10={precision10:.4f} hit_rate@10={hit_rate10:.4f}"
        )

    front = pareto_front(points)
    payload = {
        "points": points,
        "pareto_front": [p["policy"] for p in front],
        "cost_basis": "embedding token 估算 + 块数 × 100（**代理量**，非真实账单）",
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n帕累托前沿（既更便宜又更准的集合）：{payload['pareto_front']}")
    print(f"[eval] 成本-质量对照已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
