#!/usr/bin/env python3
"""索引重建：沙箱重建 → 评测比对 → 达标才原子切换别名（§4.2 + §4.3）。

**为什么需要这个脚本**：切块策略实验已经证明"默认 300/50 切得太碎"，
但实验结论**不能直接上线**——直接在生产索引上重建，期间是脏读（新旧混合），
而且重建失败等于把线上索引搞坏。

正确流程（脚本按这个顺序执行，每步都可中断）：

1. **切语料**：用生产的切块工厂（`apply_chunk_policy`）；
2. **沙箱建索引**：写到**独立的物理目录**，绝不碰生产索引；
3. **评测比对**：用同一份金标集跑 recall/nDCG，与基线比；
4. **达标才切**：只有增益达标（或显式 `--force`）才允许切换；
5. **原子切换**：改别名指针（写临时文件 + `os.replace`），秒级、可回滚。

用法：
    # 只做沙箱重建 + 评测（安全，不动生产）
    python scripts/eval/reindex.py --policy recursive-800-100

    # 达标后切换（默认要求 nDCG@10 提升 ≥ 0.02）
    python scripts/eval/reindex.py --policy recursive-800-100 --switch
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

EVAL_DIR = REPO_ROOT / "tests" / "data" / "eval"


def eval_policy(policy: str, corpus: Path, tenant: str) -> dict:
    """按策略切语料 → 建检索栈 → 跑金标集 → 返回指标。"""
    from app.services.evaluation.dataset import load_qrels, load_queries, resolve_anchors
    from app.services.evaluation.local_eval import build_retriever, run_queries, score_runs
    from app.services.evaluation.policies import chunk_corpus, resolve_policy

    resolved = resolve_policy(policy)
    chunks = chunk_corpus(corpus, policy, tenant)
    queries = load_queries(str(EVAL_DIR / "queries.jsonl"))
    anchors = load_qrels(str(EVAL_DIR / "qrels.tsv"))
    qrels = resolve_anchors(anchors, chunks)
    retriever = build_retriever(chunks)
    runs = run_queries(retriever, queries, tenant, top_k=10)
    report = score_runs(runs, qrels, (5, 10, 20))
    agg = report["aggregate"]
    return {
        "policy": policy,
        "resolved": resolved["name"],
        "chunks": len(chunks),
        "recall": agg.get("recall", {}),
        "ndcg": agg.get("ndcg", {}),
        "mrr": agg.get("mrr"),
        "union_ceiling": agg.get("union_ceiling"),
    }


def build_sandbox_index(policy: str, corpus: Path, target_dir: Path, tenant: str) -> dict:
    """在**独立目录**建一个 FAISS 索引（绝不碰生产索引）。"""
    from app.services.embeddings import MockEmbeddings
    from app.services.evaluation.policies import chunk_corpus
    from app.services.vectorstore import VectorStore

    chunks = chunk_corpus(corpus, policy, tenant)
    embeddings = MockEmbeddings(dim=1024)
    store = VectorStore(target_dir, 1024)
    store.add(chunks, embeddings.embed_texts([c.content for c in chunks]))
    store.save()
    return {"dir": str(target_dir), "chunks": len(chunks)}


def main() -> int:
    parser = argparse.ArgumentParser(description="索引重建（沙箱 + 别名切换）")
    parser.add_argument("--policy", required=True, help="切块策略预设名（如 recursive-800-100）")
    parser.add_argument("--corpus", default=str(EVAL_DIR / "corpus"))
    parser.add_argument("--tenant", default="eval-tenant")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "data"))
    parser.add_argument("--switch", action="store_true", help="达标后原子切换索引别名")
    parser.add_argument("--force", action="store_true", help="跳过达标判定直接切换（危险）")
    parser.add_argument("--min-gain", type=float, default=0.02, help="nDCG@10 最小提升")
    parser.add_argument("--out", default=str(EVAL_DIR / "reindex_report.json"))
    args = parser.parse_args()

    corpus = Path(args.corpus)
    if not corpus.exists():
        raise SystemExit(f"[reindex] 语料目录不存在：{corpus}")

    print(f"[reindex] 策略 {args.policy}：沙箱重建 + 评测")
    metrics = eval_policy(args.policy, corpus, args.tenant)

    baseline_path = EVAL_DIR / "baseline.json"
    baseline_ndcg = None
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        baseline_ndcg = (baseline.get("aggregate", {}).get("ndcg") or {}).get("10")
    gain = None
    if baseline_ndcg is not None:
        gain = float(metrics["ndcg"].get("10", 0) or 0) - float(baseline_ndcg)

    sandbox = Path(tempfile.mkdtemp(prefix="kq-reindex-"))
    try:
        sandbox_info = build_sandbox_index(args.policy, corpus, sandbox, args.tenant)
        print(f"[reindex] 沙箱索引：{sandbox_info['dir']}（{sandbox_info['chunks']} 块）")

        report = {
            "metrics": metrics,
            "baseline_ndcg10": baseline_ndcg,
            "ndcg10_gain": gain,
            "min_gain": args.min_gain,
            "switched": False,
            "sandbox": sandbox_info,
        }

        if args.switch:
            ok = args.force or (gain is not None and gain >= args.min_gain)
            if not ok:
                print(
                    f"[reindex] 未达标（增益 {gain} < 阈值 {args.min_gain}），不切换。"
                    " 确认无误可加 --force。"
                )
                report["switch_skipped"] = "not_improved"
            else:
                from app.services.index_alias import IndexAliasManager

                data_dir = Path(args.data_dir)
                data_dir.mkdir(parents=True, exist_ok=True)
                suffix = metrics["resolved"].split("_v")[-1]
                physical = f"index_v{suffix}"
                target = data_dir / physical
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(sandbox, target)
                manager = IndexAliasManager(data_dir, base_name="index")
                manager.ensure_initialized()
                status = manager.switch(physical)
                report["switched"] = True
                report["alias"] = status
                print(f"[reindex] 已切换索引别名 → {status}")

        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"[reindex] 指标：recall@10={metrics['recall'].get('10')} "
            f"nDCG@10={metrics['ndcg'].get('10')} 增益={gain}"
        )
        print(f"[reindex] 报告已写入 {args.out}")
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
