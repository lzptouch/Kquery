#!/usr/bin/env python3
"""检索评测运行器（批次 0 / 批次 1 的度量入口）。

两种模式：

1. ``--mode live``：对每条查询调后端 ``/ask``，把返回的证据顺序落成
   ``runs.jsonl``（**检索结果快照**），再按标注算指标。
   ——评测要能脱离模型环境复跑，所以先落盘再打分。
2. ``--mode file``：直接对已有的 ``runs.jsonl`` 打分（CI / 回归门禁用，
   不需要任何服务）。

用法::

    # 线上跑一遍并落盘
    python scripts/eval/run_eval.py --mode live \\
        --base-url http://127.0.0.1:8100 --user admin --password '***' \\
        --runs tests/data/eval/runs.jsonl

    # 离线复算指标（可反复跑，结果可 diff）
    python scripts/eval/run_eval.py --mode file \\
        --runs tests/data/eval/runs.jsonl --out tests/data/eval/report.json

    # CI 门禁：相对基线回退超过 2pp 即失败
    python scripts/eval/run_eval.py --mode file \\
        --runs tests/data/eval/runs.jsonl \\
        --baseline tests/data/eval/baseline.json --max-drop 0.02

零第三方依赖（只用 stdlib）：评测脚本不该因为环境问题跑不起来。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 注意：app.* 的导入放在函数内完成。脚本要把 backend 加进 sys.path 才能 import，
# 而"先改 sys.path 再 import"在模块级就是 E402；放进函数里既满足 lint，
# 又不依赖 noqa 抑制（noqa 在不同 ruff 配置下会变成"未使用的抑制"）。

# 金标集是**测试资产**（要进仓库、要在 CI 复跑），位置：tests/data/eval/
EVAL_DIR = REPO_ROOT / "tests" / "data" / "eval"
DEFAULT_QUERIES = EVAL_DIR / "queries.jsonl"
DEFAULT_QRELS = EVAL_DIR / "qrels.tsv"
DEFAULT_RUNS = EVAL_DIR / "runs.local.jsonl"
DEFAULT_OUT = EVAL_DIR / "report.local.json"
DEFAULT_CORPUS = EVAL_DIR / "corpus"


# ---------------------------------------------------------------- HTTP 客户端


def _post(url: str, payload: dict, token: str = "", timeout: float = 120.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 保留服务端错误正文，便于定位
        body = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"[eval] HTTP {exc.code} {url}: {body[:400]}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"[eval] 请求失败 {url}: {exc}")


def login(base_url: str, user: str, password: str) -> str:
    """认证已下线：不再登录，令牌恒为空。

    保留函数签名是为了不改调用面；将来恢复鉴权时把这里的登录逻辑加回来即可。
    """
    return ""


def ask(
    base_url: str, token: str, question: str, top_k: int, mode: str
) -> tuple[list[str], str]:
    """调 /ask，返回（**按相关度排序**的证据 chunk_id 列表，答案文本）。

    答案文本是给 **judge**（`EV-03`）用的：没有答案就只剩检索指标，
    「召回到了但没答对」这一侧永远是黑盒。
    """
    payload = {"question": question, "top_k": top_k, "mode": mode}
    resp = _post(f"{base_url.rstrip('/')}/ask", payload, token=token)
    data = resp.get("data") or {}
    evidence = data.get("evidence") or []
    ranked = [str(e.get("chunk_id") or "") for e in evidence if e.get("chunk_id")]
    answer = str(data.get("answer") or "")
    return ranked, answer


# ---------------------------------------------------------------- 生成侧（judge / 归因）


def judge_generation(args, runs: dict[str, dict]) -> dict:
    """生成侧判定与归因（`EV-02` rubric / `EV-03` judge / `EV-05` 归因）。

    三条纪律：

    1. **judge 跳过时必须写明原因**——「没跑」与「跑了但都是 0 分」是两件事，
       报告里混起来会让人以为生成侧质量为 0；
    2. **报告带 rubric 版本**：不同版本的分数不可比；
    3. **`heuristic` 必须标清身份**：它不是模型判定。
    """
    from app.services.evaluation.rubric import RubricError, load_rubric

    report: dict = {
        "rubric_version": "",
        "judge_kind": "none",
        "skipped": True,
        "reason": "",
        "queries": 0,
        "judged": 0,
        "low_confidence": 0,
        "failed": 0,
        "passed": 0,
        "avg_weighted": None,
        "items": [],
        "attribution": {},
    }

    try:
        rubric = load_rubric(args.rubric)
    except RubricError as exc:
        report["reason"] = f"rubric 加载失败：{exc}"
        return report
    report["rubric_version"] = rubric.version

    if args.judge == "none":
        report["reason"] = (
            "未启用 judge（--judge none）：生成侧未判定。"
            "注意这不是「生成质量为 0」，而是「没量」"
        )
        return report

    from app.services.evaluation.judge import HeuristicJudge
    from app.services.evaluation.metrics import attribution_matrix, generation_attribution

    # 问答金标（标准答案 + 要点）
    gold: dict[str, dict] = {}
    answers_path = EVAL_DIR / "answers.jsonl"
    if answers_path.exists():
        for line in answers_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            gold[str(row.get("query_id"))] = row

    judge = HeuristicJudge()
    report["judge_kind"] = judge.kind
    report["skipped"] = False
    report["reason"] = "启发式判定（确定性，非模型判定）"

    buckets: dict[str, int] = {}
    weighted_sum = 0.0
    weighted_n = 0
    for qid, run in (runs or {}).items():
        answer = str((run or {}).get("answer") or "")
        report["queries"] += 1
        if not answer:
            report["failed"] += 1
            continue
        g = gold.get(qid) or {}
        key_points = list(g.get("key_points") or [])
        result = judge.judge(
            str(g.get("question") or qid),
            answer,
            rubric,
            reference=str(g.get("answer") or ""),
            key_points=key_points,
        )
        if result.error:
            report["failed"] += 1
            continue
        report["judged"] += 1
        if result.low_confidence:
            report["low_confidence"] += 1
        if result.passed:
            report["passed"] += 1
        if result.weighted is not None:
            weighted_sum += result.weighted
            weighted_n += 1
        # 生成侧归因（`EV-05`）：检索侧的桶来自既有报告
        gen = generation_attribution(
            answer, evidence={i + 1: "" for i in range(0)}, keywords=key_points,
            judge_scores=result.dim_scores,
        )
        bucket = gen["bucket"]
        buckets[bucket] = buckets.get(bucket, 0) + 1
        # 归因矩阵：检索侧 + 生成侧 → 一句结论 + 动作（`EV-05`）
        matrix = attribution_matrix({"bucket": "found"}, gen)
        report["items"].append({
            "query_id": qid,
            "weighted": result.weighted,
            "passed": result.passed,
            "low_confidence": result.low_confidence,
            "bucket": bucket,
            "conclusion": matrix["conclusion"],
            "action": matrix["action"],
        })

    report["avg_weighted"] = round(weighted_sum / weighted_n, 4) if weighted_n else None
    report["attribution"] = {"buckets": buckets}
    return report


# ---------------------------------------------------------------- 评测主流程


def run_live(args) -> dict[str, dict]:
    from app.services.evaluation.dataset import load_queries, save_runs

    queries = load_queries(args.queries)
    if not queries:
        raise SystemExit(f"[eval] 查询集为空或不存在：{args.queries}")
    token = args.token or ""
    if not token and args.user:
        token = login(args.base_url, args.user, args.password or "")

    runs: dict[str, dict] = {}
    for i, q in enumerate(queries, start=1):
        qid = str(q["query_id"])
        ranked, answer = ask(
            args.base_url, token, str(q.get("question") or ""), args.top_k, args.ask_mode
        )
        # 答案一并落盘（judge / 生成侧归因要用；旧 runs.jsonl 没有这个字段 → 跳过并说明）
        runs[qid] = {"query_id": qid, "ranked": ranked, "answer": answer}
        print(
            f"[eval] ({i}/{len(queries)}) {qid} -> {len(ranked)} 条证据"
            f"{'，答案已采集' if answer else '，**无答案**'}",
            flush=True,
        )
    save_runs(args.runs, runs)
    print(f"[eval] 检索结果已写入 {args.runs}")
    return runs


def run_local(args) -> dict[str, dict]:
    """本地检索：离线装配检索栈（BM25 + 向量 + RRF + 重排），逐条查询后落盘。

    为什么要有这一档：`--mode live` 需要一个跑起来的后端（入库 + 真实模型），
    而 CI 与"改完 chunk 参数想立刻看数字"的场景需要**零外部依赖**的复算入口。

    口径说明（重要，别把绝对值当线上结论）：
    - **关键词路是真 BM25**（与生产同一实现）；
    - **语义路用 mock embedding**（特征哈希词袋）——确定性、无网络，但**绝对召回**
      不代表线上真实 embedding 的表现；
    - 融合与精排与生产同一实现（RRF k / 候选池 / 重排）。
    所以这个模式适合回答"分路贡献多少、并集上限在哪、缺口卡在哪一环"，
    不适合回答"线上 recall 是多少"。
    """
    from app.services.evaluation.dataset import load_queries, save_runs
    from app.services.evaluation.local_eval import build_retriever, run_queries
    from app.services.evaluation.policies import chunk_corpus, resolve_policy

    corpus_dir = Path(args.corpus)
    if not corpus_dir.exists():
        raise SystemExit(f"[eval] 语料目录不存在：{corpus_dir}")
    tenant = args.tenant
    resolved_policy = resolve_policy(args.policy)
    # 切块走**生产工厂**（apply_chunk_policy）：实验测的必须是生产路径
    chunks = chunk_corpus(corpus_dir, args.policy, tenant)
    if not chunks:
        raise SystemExit(f"[eval] 语料目录里没有可用的 .md/.txt：{corpus_dir}")
    print(
        f"[eval] 语料 {len(chunks)} 块（{corpus_dir}，策略 {resolved_policy['name']}）"
    )

    # 检索栈装配与查询执行都在 app 侧（local_eval）：脚本只做编排。
    # 两处各写一份的后果是"改了脚本忘了改库"——两边的数字会悄悄对不上。
    retriever = build_retriever(chunks, dim=args.dim)
    queries = load_queries(args.queries)
    if not queries:
        raise SystemExit(f"[eval] 查询集为空或不存在：{args.queries}")
    runs = run_queries(retriever, queries, tenant, top_k=args.top_k)
    save_runs(args.runs, runs)
    print(f"[eval] 检索结果已写入 {args.runs}")
    return runs


def compare_with_baseline(report: dict, baseline_path: str, max_drop: float) -> list[str]:
    """与基线比较，返回**劣化项**描述列表（空列表 = 通过）。"""
    path = Path(baseline_path)
    if not path.exists():
        return [f"基线文件不存在：{baseline_path}（首次运行请先落基线）"]
    try:
        base = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"基线文件无法解析：{exc}"]

    problems: list[str] = []
    agg = report.get("aggregate") or {}
    base_agg = base.get("aggregate") or {}
    for metric in ("recall", "ndcg"):
        for k, value in (agg.get(metric) or {}).items():
            old = (base_agg.get(metric) or {}).get(k)
            if old is None:
                continue
            if float(value) < float(old) - max_drop:
                problems.append(
                    f"{metric}@{k} 回退：{old} → {value}（超过阈值 {max_drop}）"
                )
    return problems


def print_report(report: dict) -> None:
    agg = report.get("aggregate") or {}
    print("\n=== 检索评测报告 ===")
    print(f"查询数：{agg.get('queries', 0)}")
    recall = agg.get("recall") or {}
    ndcg = agg.get("ndcg") or {}
    if recall:
        ks = sorted(recall, key=lambda x: int(x))
        print("  k    recall   nDCG")
        for k in ks:
            print(f"  {k:<4} {recall[k]:<8} {ndcg.get(k, '-')}")
    if agg.get("mrr") is not None:
        print(f"  MRR  {agg.get('mrr')}")
    attribution = report.get("attribution") or {}
    if attribution:
        print(
            "  缺口归因：未召回 {not_recalled} / 召回未进融合 {recalled_not_fused} / "
            "进融合未进最终 {fused_not_final}".format(**attribution)
        )
    answerable_rate = agg.get("text_answerable_rate")
    if answerable_rate is not None:
        print(
            f"  文本可答性：{answerable_rate}"
            f"（1.0 = 检索到的文字就能答；越低说明越多题**必须看原图**）"
        )

    by_kind = agg.get("by_kind") or {}
    if by_kind:
        print("  按题型分组（k=10）：")
        for kind, data in by_kind.items():
            label = "必须看图" if kind == "vision" else "文本可答"
            print(
                f"    {label:<8} n={data['count']:<3}"
                f"recall@10={data['recall'].get('10')}  nDCG@10={data['ndcg'].get('10')}"
            )

    routes = agg.get("route_recall") or {}
    ceiling = agg.get("union_ceiling") or {}
    headroom = agg.get("fusion_headroom") or {}
    if routes:
        print("  分路 recall / 并集上限：")
        for k in sorted(routes, key=lambda x: int(x)):
            for name, v in routes[k].items():
                print(f"    k={k:<3} {name:<10} recall={v}")
        if isinstance(ceiling, (int, float)):
            print(
                f"    并集上限（候选池）= {ceiling}"
                f"（融合还能买 {headroom.get('10', 0)}）"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Kquery 检索评测运行器")
    parser.add_argument("--mode", choices=("file", "live", "local"), default="file")
    parser.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="local 模式的语料目录")
    parser.add_argument("--policy", default="", help="切块策略：预设名或 JSON（默认 recursive-300-50）")
    parser.add_argument("--dim", type=int, default=1024, help="mock embedding 维度")
    parser.add_argument("--tenant", default="eval-tenant")
    parser.add_argument("--queries", default=str(DEFAULT_QUERIES))
    parser.add_argument("--qrels", default=str(DEFAULT_QRELS))
    parser.add_argument("--runs", default=str(DEFAULT_RUNS))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--baseline", default="", help="基线报告 JSON（用于回退门禁）")
    parser.add_argument("--max-drop", type=float, default=0.02, help="允许的回退幅度（默认 2pp）")
    parser.add_argument("--k", default="5,10,20", help="评估的 k 列表，逗号分隔")
    parser.add_argument("--top-k", type=int, default=10, help="live 模式向 /ask 请求的条数")
    # 生成侧（`EV-02` / `EV-03` / `EV-05`）
    parser.add_argument(
        "--rubric", default=None,
        help="评分标准文件（默认 tests/data/eval/rubric.v1.json）；报告里会带 rubric 版本",
    )
    parser.add_argument(
        "--judge", default="none", choices=("none", "heuristic"),
        help="生成侧判定：none=跳过（默认）；heuristic=启发式（CI 安全，非模型判定）",
    )
    parser.add_argument(
        "--judge-report", default=str(EVAL_DIR / "judge_report.json"),
        help="生成侧判定报告的输出路径",
    )
    parser.add_argument("--ask-mode", default="auto", choices=("auto", "agent"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8100")
    parser.add_argument("--token", default="", help="直接给 Bearer 令牌（跳过登录）")
    parser.add_argument("--user", default="", help="口令模式登录用户名")
    parser.add_argument("--password", default="", help="口令模式登录口令")
    args = parser.parse_args()

    k_values = tuple(int(x) for x in args.k.split(",") if x.strip())

    from app.services.evaluation.dataset import load_qrels, load_runs, resolve_anchors
    from app.services.evaluation.local_eval import score_runs
    from app.services.evaluation.policies import chunk_corpus

    if args.mode == "live":
        runs = run_live(args)
    elif args.mode == "local":
        runs = run_local(args)
    else:
        runs = load_runs(args.runs)
    if not runs:
        raise SystemExit(f"[eval] 没有检索结果可评测：{args.runs}（先跑 --mode live）")

    anchor_qrels = load_qrels(args.qrels)
    if not anchor_qrels:
        raise SystemExit(f"[eval] 没有标注数据：{args.qrels}")

    # 标注存的是**内容锚点** → 按当前切块解析成 chunk_id（与切块方式无关，可跨策略复用）
    chunks = chunk_corpus(Path(args.corpus), args.policy, args.tenant)
    qrels = resolve_anchors(anchor_qrels, chunks)
    missing = sorted(set(anchor_qrels) - set(qrels))
    if missing:
        print(
            f"[eval] 警告：{len(missing)} 条标注的锚点未命中任何块（已剔除）：{', '.join(missing[:5])}",
            file=sys.stderr,
        )
        # 图文语料走 captions/ 下的 img-* 锚点：漏传 --corpus 会全落空，给个能自救的提示
        if any(qid.startswith("i") for qid in missing):
            print(
                "[eval] 提示：这些标注指向图文语料 —— 评测图文集时请用 --corpus 指定语料目录",
                file=sys.stderr,
            )
    if not qrels:
        raise SystemExit(
            "[eval] 标注全部失效（锚点没命中任何块）。"
            "检查：① --corpus 是否指向了正确的语料目录；② 语料是否被改写过。"
        )

    # 题型分组（"vision" = 必须看图才能答的对抗题）
    kinds: dict[str, str] = {}
    try:
        from app.services.evaluation.dataset import load_queries

        for q in load_queries(args.queries):
            if q.get("requires_vision"):
                kinds[str(q.get("query_id"))] = "vision"
    except Exception:  # noqa: BLE001 - 题型只是分组维度，取不到就退化为单一分组
        kinds = {}

    # 答案关键词 + 块文本（用于「文本可答性」：检索到的文字里到底有没有答案）
    answers: dict[str, str] = {}
    try:
        from app.services.evaluation.dataset import load_queries as _lq

        for q in _lq(args.queries):
            if q.get("answer_key"):
                answers[str(q.get("query_id"))] = str(q["answer_key"])
    except Exception:  # noqa: BLE001
        answers = {}
    chunk_text: dict[str, str] = {}
    if answers:
        for c in chunks:
            chunk_text[c.chunk_id] = c.content or ""

    report = score_runs(
        runs, qrels, k_values, kinds=kinds, answers=answers, chunk_text=chunk_text
    )
    print_report(report)

    # 与基线的差异是否显著（`EV-04`）：**只打标，不作为失败条件**
    # （噪声大、容易误伤；是否改基线由人决定）。
    if args.baseline and Path(args.baseline).exists():
        try:
            from app.services.evaluation.significance import as_dict, paired_bootstrap

            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            base_pq = (baseline.get("per_query_ndcg10") or {})
            cur_pq = {
                qid: float((row.get("ndcg") or {}).get("10") or 0.0)
                for qid, row in (report.get("per_query") or {}).items()
            }
            common = sorted(set(base_pq) & set(cur_pq))
            if common:
                sig = paired_bootstrap(
                    [cur_pq[q] for q in common], [base_pq[q] for q in common]
                )
                report["significance_vs_baseline"] = as_dict(sig)
                print(
                    f"[eval] 与基线的 nDCG@10 差异：{sig.mean_diff:+.4f}"
                    f"（95% CI [{sig.ci_low}, {sig.ci_high}]，p={sig.p_value}，n={sig.sample_size}）"
                    + ("　**显著**" if sig.significant else "　不显著")
                    + (f"　{sig.note}" if sig.note else "")
                )
        except Exception as exc:  # noqa: BLE001 - 显著性是附加维度，失败不该毁掉评测
            print(f"[eval] 显著性计算失败（忽略）：{exc}", file=sys.stderr)

    # 生成侧（judge / 归因）：独立报告，不混进检索指标
    judge_report = judge_generation(args, runs)
    judge_out = Path(args.judge_report)
    judge_out.parent.mkdir(parents=True, exist_ok=True)
    judge_out.write_text(json.dumps(judge_report, ensure_ascii=False, indent=2), encoding="utf-8")
    if judge_report["skipped"]:
        print(f"[eval] 生成侧判定跳过：{judge_report['reason']}")
    else:
        print(
            f"[eval] 生成侧判定（{judge_report['judge_kind']}，rubric {judge_report['rubric_version']}）："
            f"判定 {judge_report['judged']}/{judge_report['queries']} 条，"
            f"通过 {judge_report['passed']}，平均分 {judge_report['avg_weighted']}"
        )
    print(f"[eval] 生成侧报告已写入 {judge_out}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval] 报告已写入 {out}")

    if args.baseline:
        problems = compare_with_baseline(report, args.baseline, args.max_drop)
        if problems:
            print("[eval] 未通过回退门禁：", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 1
        print("[eval] 回退门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
