#!/usr/bin/env python
"""`ST-01` 阶段 B 的前后对比门禁：同一批证据、同一 judge、两种渲染。

## 为什么需要这个脚本

阶段 B 把提示词从「单段拼接」改成「五来源分段渲染」。这是**会改变答案质量**的
改动，而设计文档 §2.1 第 7 条明确要求：切换默认渲染模式必须由 judge 数据驱动，
不能靠拍脑袋。这个脚本就是那个「数据」的产出入口。

三条纪律（与 `run_eval.py` 的生成侧判定一致）：

1. **两种模式喂同一批证据**：证据取自同一次检索结果，只变渲染，
   否则「结构化更好」可能只是「这次检索运气好」；
2. **judge 与检索解耦**：判定只看答案，不看渲染方式（judge 不知道模式，避免偏置）；
3. **不劣化即通过**：门禁判据是「structured 相对 legacy 不显著变差」，
   而不是「structured 必须更好」——结构分区的收益是**可维护性与可控性**，
   不该被要求顺带提升分数。

## 用法

```bash
# 离线冒烟（MockLLM + 启发式 judge）：只验证管线通不通，分数不可用于结论
backend/.venv/bin/python scripts/eval/partition_ab.py --offline --limit 8

# 真实模型（默认走 backend/.env 的 MODEL_PROVIDER）
backend/.venv/bin/python scripts/eval/partition_ab.py --limit 60

# 只看结构化模式，不跑对比（调试渲染用）
backend/.venv/bin/python scripts/eval/partition_ab.py --only structured --dump-prompt
```

产物：`tests/data/eval/partition_ab.json`（含逐条分数、逐维度均值、
配对 bootstrap 的 p 值与门禁结论）。

退出码：0 = 通过（structured 未显著变差）；1 = 门禁不通过；2 = 无法评测（缺模型/金标）。
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

EVAL_DIR = ROOT / "tests" / "data" / "eval"
DEFAULT_GOLDSET = EVAL_DIR / "answers.jsonl"
DEFAULT_CORPUS = EVAL_DIR / "corpus"
DEFAULT_OUT = EVAL_DIR / "partition_ab.json"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="ST-01 阶段 B：legacy vs structured 渲染对比")
    p.add_argument("--goldset", default=str(DEFAULT_GOLDSET), help="问答金标（answers.jsonl）")
    p.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="语料目录（本地检索用）")
    p.add_argument("--policy", default="", help="切块策略（默认 recursive-300-50）")
    p.add_argument("--rubric", default=None, help="评分标准（默认 rubric.v1.json）")
    p.add_argument("--top-k", type=int, default=5, help="每题的证据条数（两模式一致）")
    p.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 = 全量）")
    p.add_argument("--concurrency", type=int, default=4, help="并发生成数")
    p.add_argument("--offline", action="store_true", help="用 MockLLM（分数仅验证管线）")
    p.add_argument("--only", choices=("legacy", "structured"), default="", help="只跑一种模式")
    p.add_argument("--dump-prompt", action="store_true", help="把首条提示词打到 stdout")
    p.add_argument("--max-drop", type=float, default=0.02, help="允许的回退幅度（默认 2pp）")
    p.add_argument(
        "--from-report", default="",
        help="按已落盘的报告复算比较（不重跑生成；换阈值时用这个）",
    )
    p.add_argument("--out", default=str(DEFAULT_OUT))
    return p.parse_args()


# ---------------------------------------------------------------- 装配


def load_goldset(path: Path, limit: int = 0) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"[ab] 金标不存在：{path}")
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("question"):
            rows.append(row)
    return rows[:limit] if limit > 0 else rows


def build_evidence_index(corpus_dir: Path, policy: str, tenant: str):
    """本地检索栈（与 `run_eval.py --mode local` 同一实现）。

    用本地档而不是 live 档：live 需要跑起来的后端 + 真实向量库，
    一个「判断提示词结构有没有搞坏答案」的门禁不该依赖半套基础设施。
    """
    from app.services.evaluation.local_eval import build_retriever
    from app.services.evaluation.policies import chunk_corpus

    if not corpus_dir.exists():
        raise SystemExit(f"[ab] 语料目录不存在：{corpus_dir}")
    chunks = chunk_corpus(corpus_dir, policy, tenant)
    if not chunks:
        raise SystemExit(f"[ab] 语料目录里没有可用的 .md/.txt：{corpus_dir}")
    return build_retriever(chunks)


def collect_evidence(retriever, question: str, tenant: str, top_k: int) -> list:
    from app.domain.schemas import Evidence, RetrievalConfig

    result = retriever.search(question, tenant, RetrievalConfig(), top_k=top_k)
    evidence: list[Evidence] = []
    for i, cand in enumerate(result.candidates, start=1):
        chunk = cand.chunk
        route = cand.source_route or "vector"
        # 分数取重排分优先：与生产提示词里的 score 口径一致（重排后 rrf 分不再可比）
        score = cand.rerank_score if cand.rerank_score is not None else cand.rrf_score
        evidence.append(
            Evidence(
                rank=i,
                chunk_id=chunk.chunk_id,
                score=float(score or 0.0),
                content=chunk.content or "",
                source=chunk.source or route,
                doc_id=chunk.doc_id or "",
                filename=chunk.doc_id or chunk.chunk_id,
                source_route=route,
            )
        )
    return evidence


def build_llm(offline: bool):
    from app.core.config import Settings
    from app.services.llm import MockLLM, build_llm as _build

    settings = Settings()
    if offline:
        return MockLLM(), "mock"
    llm = _build(
        provider=settings.resolved_provider,
        api_key=settings.resolved_llm_api_key,
        model=settings.llm_model,
        temperature=settings.temperature,
        max_retries=settings.llm_max_retries,
        timeout_s=settings.llm_timeout_s,
        base_url=settings.resolved_llm_base_url,
    )
    return llm, f"{settings.resolved_provider}:{settings.llm_model}"


# ---------------------------------------------------------------- 主流程


def render_for(question: str, evidence: list, mode: str) -> str:
    from app.services.context.render import render_prompt

    return render_prompt(question, evidence, structured=(mode == "structured"))


def score_answer(judge, rubric, row: dict, answer: str):
    result = judge.judge(
        str(row.get("question") or ""),
        answer,
        rubric,
        reference=str(row.get("answer") or ""),
        key_points=list(row.get("key_points") or []),
    )
    return {
        "answer": answer,
        "weighted": result.weighted,
        "passed": result.passed,
        "low_confidence": result.low_confidence,
        "error": result.error,
        "dim_scores": dict(result.dim_scores or {}),
    }


def summarize(mode: str, items: list[dict]) -> dict:
    judged = [it for it in items if it["weighted"] is not None and not it.get("error")]
    dims: dict[str, list[float]] = {}
    for it in judged:
        for dim, value in (it.get("dim_scores") or {}).items():
            if value is not None:
                dims.setdefault(dim, []).append(float(value))
    return {
        "mode": mode,
        "queries": len(items),
        "judged": len(judged),
        "failed": len(items) - len(judged),
        "passed": sum(1 for it in judged if it.get("passed")),
        "avg_weighted": (
            round(sum(it["weighted"] for it in judged) / len(judged), 4) if judged else None
        ),
        "dims": {d: round(sum(v) / len(v), 4) for d, v in dims.items()},
    }


def compare(items_by_mode: dict[str, dict[str, dict]], max_drop: float) -> dict:
    """逐题配对比较 + 配对 bootstrap（`EV-04` 同一实现）。

    **判据分两级**（这是本脚本唯一需要解释清楚的判断）：

    - `regressions`（门禁不通过）：幅度超阈值 **且** 显著（p < 0.05）——
      幅度说明"差多少"，显著性说明"这个差是不是噪声"；
    - `warnings`（门禁通过但留痕）：幅度超阈值、但配对 bootstrap 判为不显著——
      60 题量级上 2pp 的抖动完全可能是采样噪声，直接判死会让门禁变成
      "谁能刷到好运气谁过"。留痕是为了让人知道「这里曾经掠过阈值」。

    反过来说：**幅度没超阈值的正向/负向变化都只是噪声，不打标**。
    """
    from app.services.evaluation.significance import as_dict, paired_bootstrap

    report: dict = {"dims": {}, "regressions": [], "warnings": [], "passed": True}
    ids = sorted(set(items_by_mode.get("legacy", {})) & set(items_by_mode.get("structured", {})))
    pairs = [
        (items_by_mode["legacy"][q], items_by_mode["structured"][q]) for q in ids
    ]
    pairs = [(a, b) for a, b in pairs if a["weighted"] is not None and b["weighted"] is not None]
    if not pairs:
        report["passed"] = False
        report["regressions"].append("没有可配对的判定结果（两模式都必须有有效分数）")
        return report

    def judge_drop(label: str, base: float, cur: float, sig) -> None:
        delta = cur - base
        if delta >= -max_drop:
            return
        line = (
            f"{label} 回退 {delta:+.4f}（{base:.3f} → {cur:.3f}，阈值 {max_drop}，"
            f"p={sig.get('p_value')}，n={sig.get('sample_size')}）"
        )
        if sig.get("significant"):
            report["regressions"].append(line + "　**显著**")
        else:
            report["warnings"].append(line + "　不显著，仅留痕")

    # 整体加权分
    a_scores = [a["weighted"] for a, _ in pairs]
    b_scores = [b["weighted"] for _, b in pairs]
    overall_sig = as_dict(paired_bootstrap(b_scores, a_scores))
    report["weighted"] = {
        "legacy": round(sum(a_scores) / len(a_scores), 4),
        "structured": round(sum(b_scores) / len(b_scores), 4),
        "delta": round(
            sum(b_scores) / len(b_scores) - sum(a_scores) / len(a_scores), 4
        ),
        "significance": overall_sig,
    }
    judge_drop(
        "加权总分",
        report["weighted"]["legacy"],
        report["weighted"]["structured"],
        overall_sig,
    )

    # 逐维度
    dim_names = sorted({d for a, b in pairs for d in (a.get("dim_scores") or {})})
    for dim in dim_names:
        a_vals, b_vals = [], []
        for a, b in pairs:
            av = (a.get("dim_scores") or {}).get(dim)
            bv = (b.get("dim_scores") or {}).get(dim)
            if av is None or bv is None:
                continue
            a_vals.append(float(av))
            b_vals.append(float(bv))
        if not a_vals:
            continue
        base = sum(a_vals) / len(a_vals)
        cur = sum(b_vals) / len(b_vals)
        entry = {
            "legacy": round(base, 4),
            "structured": round(cur, 4),
            "delta": round(cur - base, 4),
            "n": len(a_vals),
            "significance": as_dict(paired_bootstrap(b_vals, a_vals)),
        }
        report["dims"][dim] = entry
        judge_drop(dim, base, cur, entry["significance"])

    report["passed"] = not report["regressions"]
    return report


def print_comparison(cmp_: dict) -> None:
    print("\n=== 渲染模式对比（legacy → structured） ===")
    for dim, entry in (cmp_.get("dims") or {}).items():
        sig = entry["significance"]
        print(
            f"  {dim}: {entry['legacy']:.3f} → {entry['structured']:.3f}"
            f"（{entry['delta']:+.4f}，p={sig.get('p_value')}，n={entry['n']}）"
        )
    w = cmp_.get("weighted") or {}
    if w:
        print(f"  加权总分：{w['legacy']:.3f} → {w['structured']:.3f}（{w['delta']:+.4f}）")
    for note in cmp_.get("warnings") or []:
        print(f"[ab] 留痕（不显著）：{note}")
    if cmp_.get("passed", False):
        print("[ab] 门禁通过：structured 未显著劣化")
    else:
        for problem in cmp_.get("regressions") or []:
            print(f"[ab] 门禁不通过：{problem}", file=sys.stderr)


def main() -> int:
    args = parse_args()

    from app.services.evaluation.judge import HeuristicJudge
    from app.services.evaluation.rubric import RubricError, load_rubric

    if args.from_report:
        # 复算模式：只按**已落盘的分数**重新比较（换阈值 / 补显著性），不重跑生成。
        # 有了它，「阈值调紧一点会不会过」不必再花一轮 120 次模型调用。
        prior = json.loads(Path(args.from_report).read_text(encoding="utf-8"))
        prior["max_drop"] = args.max_drop
        prior["comparison"] = compare(prior.get("items") or {}, args.max_drop)
        print_comparison(prior["comparison"])
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(prior, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ab] 报告已写入 {out}（复算，未调用模型）")
        return 0 if prior["comparison"]["passed"] else 1

    try:
        rubric = load_rubric(args.rubric)
    except RubricError as exc:
        print(f"[ab] rubric 加载失败：{exc}", file=sys.stderr)
        return 2

    gold = load_goldset(Path(args.goldset), args.limit)
    if not gold:
        print("[ab] 金标为空", file=sys.stderr)
        return 2

    tenant = "eval-tenant"
    retriever = build_evidence_index(Path(args.corpus), args.policy, tenant)
    llm, llm_label = build_llm(args.offline)
    if llm_label == "mock":
        print("[ab] 警告：MockLLM 只会复述证据，两模式分数必然接近——仅验证管线，不可作为结论")

    judge = HeuristicJudge()
    modes = [args.only] if args.only else ["legacy", "structured"]

    # 每题先检索一次，两种模式共用同一批证据（配对口径的前提）
    prepared: list[tuple[dict, list]] = [
        (row, collect_evidence(retriever, str(row["question"]), tenant, args.top_k))
        for row in gold
    ]
    if args.dump_prompt and prepared:
        from app.services.context.render import render_prompt

        row, evidence = prepared[0]
        print("=" * 60)
        print(render_prompt(str(row["question"]), evidence, structured=(modes[-1] == "structured")))
        print("=" * 60)

    items_by_mode: dict[str, dict[str, dict]] = {}
    for mode in modes:
        print(f"[ab] 生成中：{mode}（{len(prepared)} 题，并发 {args.concurrency}）", flush=True)

        def work(row: dict, evidence: list) -> tuple[str, dict]:
            prompt = render_for(str(row["question"]), evidence, mode)
            try:
                answer = llm.generate(prompt)
            except Exception as exc:  # noqa: BLE001 - 单题失败不该毁掉整轮评测
                return str(row.get("query_id")), {
                    "answer": "", "weighted": None, "passed": None, "dim_scores": {},
                    "error": f"生成失败：{exc}", "low_confidence": False,
                }
            return str(row.get("query_id")), score_answer(judge, rubric, row, answer)

        results: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            for qid, scored in pool.map(lambda pair: work(*pair), prepared):
                results[qid] = scored
        items_by_mode[mode] = results
        summary = summarize(mode, list(results.values()))
        print(
            f"[ab] {mode}：判定 {summary['judged']}/{summary['queries']} 条，"
            f"通过 {summary['passed']}，平均分 {summary['avg_weighted']}"
        )

    report: dict = {
        "rubric_version": rubric.version,
        "judge_kind": judge.kind,
        "llm": llm_label,
        "offline": bool(args.offline),
        "top_k": args.top_k,
        "max_drop": args.max_drop,
        "summaries": {m: summarize(m, list(items.values())) for m, items in items_by_mode.items()},
        "items": {
            m: {qid: it for qid, it in items.items()} for m, items in items_by_mode.items()
        },
    }

    if len(modes) == 2:
        report["comparison"] = compare(items_by_mode, args.max_drop)
        print_comparison(report["comparison"])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ab] 报告已写入 {out}")

    if len(modes) == 2 and not report["comparison"]["passed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
