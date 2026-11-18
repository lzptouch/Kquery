"""检索评测指标：recall@k / nDCG@10 / MRR + 分路与并集上限 + 缺口归因。

**为什么要有这一层**：此前项目里没有任何检索质量的度量口径，
"召回率还有多少提升空间""chunk 策略谁更好"只能靠感觉调参。
而**错误的数字比没有数字更危险**——它会被用来做参数决策。
所以这里的每个指标都是纯函数、无依赖，且都配了可手工验算的单测。

术语：
- grade：标注等级（2=该块独立足以回答，1=相关但不足以独立回答，0=不相关）
- relevant：grade >= 1 的块集合
- routes：各召回路的原始结果（`{路名: [chunk_id, ...]}`），用于分路/并集分析
"""
from __future__ import annotations

import math

GRADE_MIN_RELEVANT = 1  # grade >= 1 视为"相关"（recall 用）

DEFAULT_K_VALUES = (5, 10, 20)


def relevant_ids(grades: dict[str, int]) -> set[str]:
    """相关块集合（grade >= 1）。"""
    return {cid for cid, g in (grades or {}).items() if int(g or 0) >= GRADE_MIN_RELEVANT}


def recall_at_k(ranked: list[str], rel: set[str], k: int) -> float:
    """recall@k = top-k 命中的相关块 / 全部相关块。

    没有相关块时返回 0.0（而不是除零或 1.0）：该查询对指标无贡献，
    调用方应通过 `n_relevant` 把它识别出来。
    """
    if not rel or k <= 0:
        return 0.0
    hit = len({cid for cid in (ranked or [])[:k] if cid in rel})
    return hit / len(rel)


def dcg_at_k(ranked: list[str], grades: dict[str, int], k: int) -> float:
    """DCG@k（分级增益 2^g - 1，位置折损 log2(i+2)）。"""
    total = 0.0
    for i, cid in enumerate((ranked or [])[: max(0, k)]):
        g = int((grades or {}).get(cid, 0) or 0)
        if g > 0:
            total += (2 ** g - 1) / math.log2(i + 2)
    return total


def idcg_at_k(grades: dict[str, int], k: int) -> float:
    """理想 DCG（按 grade 降序排列后的 DCG），用于归一化。"""
    ideal = sorted(
        (int(g or 0) for g in (grades or {}).values() if int(g or 0) > 0),
        reverse=True,
    )[: max(0, k)]
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))


def ndcg_at_k(ranked: list[str], grades: dict[str, int], k: int) -> float:
    """nDCG@k ∈ [0,1]。没有相关块时为 0.0。"""
    idcg = idcg_at_k(grades, k)
    if idcg <= 0:
        return 0.0
    return dcg_at_k(ranked, grades, k) / idcg


def precision_at_k(ranked: list[str], rel: set[str], k: int) -> float:
    """precision@k = 命中数 / min(k, 实际候选数)（`EV-06`）。

    ## 口径说明（重要）

    分母用 **min(k, 实际候选数)** 而不是固定 k：检索只召回了 3 条时，
    分母若固定为 10，则「3 条全命中」的 precision 是 0.3 —— 这不是模型的问题，
    是候选池不够。除以实际候选数才是「召回的东西里有多少是对的」。

    没有候选 → 返回 0.0（不返回 1.0：那会把「没召回」算成「全对」）。
    """
    if k <= 0:
        return 0.0
    top = list(ranked or [])[:k]
    if not top:
        return 0.0
    hit = len({cid for cid in top if cid in rel})
    return hit / min(k, len(top))


def hit_rate_at_k(ranked: list[str], rel: set[str], k: int) -> float:
    """hit_rate@k = 至少命中一个金标的查询占比（`EV-06`）。

    与 recall@k 的区别：recall 看「找到了多少」，hit_rate 看「找到了没有」——
    **用户视角的「答得上吗」**。一个查询里即使只命中 1/3 的金标，
    只要命中了，用户就可能得到有用的答案。
    """
    if k <= 0:
        return 0.0
    top = list(ranked or [])[:k]
    return 1.0 if any(cid in rel for cid in top) else 0.0


def mrr(ranked: list[str], rel: set[str]) -> float:
    """MRR：第一个相关块排名的倒数（没命中为 0）。"""
    if not rel:
        return 0.0
    for i, cid in enumerate(ranked or [], start=1):
        if cid in rel:
            return 1.0 / i
    return 0.0


def route_recall(routes: dict[str, list[str]], rel: set[str], k: int) -> dict[str, float]:
    """分路 recall@k：每一路单独能召回多少。

    用途：判断"值不值得再加一路召回"——一路的边际贡献接近 0 就别加。
    """
    return {name: recall_at_k(ids or [], rel, k) for name, ids in (routes or {}).items()}


def union_ceiling(routes: dict[str, list[str]], rel: set[str]) -> float:
    """并集上限：**融合候选池**能召回多少 —— 当前召回策略的理论天花板。

    口径（重要）：候选池 = **各路完整候选列表的并集**（每路 up to per_route_top_n），
    而不是"各路 top-k 的并集"。后者比融合池小得多，会让天花板**低于**实际 recall，
    得出"融合还能买 -0.06"这种自相矛盾的负数。

    天花板与实际 recall@10 的差 = "融合/精排还能买多少"的空间。
    """
    if not rel:
        return 0.0
    union: set[str] = set()
    for ids in (routes or {}).values():
        union |= set(ids or [])
    return len(union & rel) / len(rel)


def gap_attribution(
    routes: dict[str, list[str]],
    fused: list[str],
    final: list[str],
    rel: set[str],
) -> dict[str, int]:
    """缺口归因：没进最终结果的答案块卡在哪一环。

    - ``not_recalled``：任何一路都没召回 → 召回问题（加路 / 换 chunk / 换 embedding）
    - ``recalled_not_fused``：召回到了但没进融合集 → 融合问题（RRF 参数 / 候选池）
    - ``fused_not_final``：进了融合集但没进最终 Top-K → 精排或 top_k 问题
    """
    route_union: set[str] = set()
    for ids in (routes or {}).values():
        route_union |= set(ids or [])
    fused_set = set(fused or [])
    final_set = set(final or [])
    out = {
        "not_recalled": 0,
        "recalled_not_fused": 0,
        "fused_not_final": 0,
        "found": 0,
    }
    for cid in rel or set():
        if cid in final_set:
            out["found"] += 1
        elif cid in route_union and cid not in fused_set:
            out["recalled_not_fused"] += 1
        elif cid in fused_set:
            out["fused_not_final"] += 1
        else:
            out["not_recalled"] += 1
    return out


def generation_attribution(
    answer: str,
    evidence: dict[int, str],
    keywords: list[str] | None = None,
    judge_scores: dict | None = None,
) -> dict:
    """生成侧归因（`EV-05`）：答案在**引用 / 忠实性 / 正确性**上卡在哪一环。

    | 桶 | 含义 |
    |---|---|
    | `not_cited` | 证据进了上下文，但答案**没有引用**它 |
    | `misquoted` | 引用了，但与证据矛盾（逐条做子串 / 要点判定） |
    | `answered_wrong` | 引用了也一致，但结论错（靠 judge 的「答案正确性」维度） |
    | `correct` | 引用一致且结论正确 |
    | `unjudged` | **没有 judge 结果** → 诚实标注，**不计入任何桶** |

    `unjudged` 是必须的：没有 judge 时**不能假装答对或答错**——
    把「没判定」当成「答对」会让生成侧归因永远显示「没问题」。
    """
    # 按需载入：引用检查只在生成侧归因时用到（纯检索评测不必付导入成本）
    from app.services.evaluation.citations import check_citations

    report = check_citations(answer or "", evidence or {}, keywords=keywords)

    if not report.cited:
        bucket = "not_cited"
    elif report.fabricated or any(not c.supported for c in report.checks):
        bucket = "misquoted"
    elif judge_scores is None:
        bucket = "unjudged"
    else:
        correctness = judge_scores.get("answer_correctness")
        bucket = "correct" if correctness is not None and correctness >= 0.8 else "answered_wrong"

    return {
        "bucket": bucket,
        "cited": report.cited,
        "uncited_evidence": report.uncited_evidence,
        "fabricated": report.fabricated,
        "checks": [
            {"number": c.number, "supported": c.supported, "reason": c.reason}
            for c in report.checks
        ],
    }


def attribution_matrix(retrieval: dict, generation: dict | None) -> dict:
    """检索 4 桶 × 生成侧的归因矩阵 + 一句可读的结论（`EV-05`）。

    `generation` 为 None（无 judge 结果 / local 模式无生成结果）→
    生成侧**不产出**，并在结论里写明原因（不假装答对了）。
    """
    if generation is None:
        conclusion = {
            "not_recalled": "召回是瓶颈",
            "recalled_not_fused": "融合是瓶颈",
            "fused_not_final": "精排是瓶颈",
            "found": "检索命中，但生成侧未判定（无 judge 结果）",
        }[retrieval.get("bucket", "found")]
        return {
            "retrieval": retrieval,
            "generation": None,
            "conclusion": conclusion,
            "action": "补 judge 结果后再看生成侧",
        }

    bucket = generation.get("bucket", "unjudged")
    if retrieval.get("bucket") != "found":
        conclusion = {
            "not_recalled": "召回是瓶颈",
            "recalled_not_fused": "融合是瓶颈",
            "fused_not_final": "精排是瓶颈",
            "found": "检索命中",
        }[retrieval.get("bucket", "found")]
        action = "先解决检索侧（生成侧结论在检索没命中时没有意义）"
    else:
        conclusion = {
            "not_cited": "引用是瓶颈",
            "misquoted": "忠实性是瓶颈",
            "answered_wrong": "生成是瓶颈",
            "correct": "通过",
            "unjudged": "未判定（无 judge 结果）",
        }[bucket]
        action = {
            "not_cited": "改证据块格式 / 提示词要求引用",
            "misquoted": "收紧自检 / 加引用校验",
            "answered_wrong": "换模型 / 改提示词 / 加 verify",
            "correct": "——",
            "unjudged": "补 judge 结果",
        }[bucket]
    return {
        "retrieval": retrieval,
        "generation": generation,
        "conclusion": conclusion,
        "action": action,
    }


def evaluate_query(
    ranked: list[str],
    grades: dict[str, int],
    k_values=DEFAULT_K_VALUES,
) -> dict:
    """单条查询的指标包。"""
    rel = relevant_ids(grades)
    res: dict = {
        "mrr": round(mrr(ranked, rel), 4),
        "n_relevant": len(rel),
        "n_ranked": len(ranked or []),
        "recall": {},
        "ndcg": {},
        # `EV-06`：补两个纯函数指标
        "precision": {},
        "hit_rate": {},
    }
    for k in k_values:
        res["recall"][str(k)] = round(recall_at_k(ranked, rel, k), 4)
        res["ndcg"][str(k)] = round(ndcg_at_k(ranked, grades, k), 4)
        res["precision"][str(k)] = round(precision_at_k(ranked, rel, k), 4)
        res["hit_rate"][str(k)] = round(hit_rate_at_k(ranked, rel, k), 4)
    return res


def aggregate(results: list[dict]) -> dict:
    """宏平均：每条查询等权。

    刻意不用微平均（按相关块总数加权）：那样长文档题会主导整体数字，
    掩盖"多数查询其实召不回"的真实情况。
    """
    results = [r for r in (results or []) if r]
    if not results:
        return {"queries": 0, "recall": {}, "ndcg": {}, "mrr": 0.0}
    ks = sorted({k for r in results for k in (r.get("recall") or {})}, key=lambda x: int(x))
    out: dict = {"queries": len(results)}
    out["recall"] = {
        k: round(
            sum(float(r["recall"][k]) for r in results if k in (r.get("recall") or {}))
            / len(results),
            4,
        )
        for k in ks
    }
    out["ndcg"] = {
        k: round(
            sum(float(r["ndcg"][k]) for r in results if k in (r.get("ndcg") or {}))
            / len(results),
            4,
        )
        for k in ks
    }
    out["mrr"] = round(sum(float(r.get("mrr", 0.0)) for r in results) / len(results), 4)
    # `EV-06`：precision / hit_rate 的聚合（口径与 recall 一致：宏平均）
    for metric in ("precision", "hit_rate"):
        metric_ks = sorted(
            {k for r in results for k in (r.get(metric) or {})}, key=lambda x: int(x)
        )
        if not metric_ks:
            continue
        out[metric] = {
            k: round(
                sum(
                    float(r[metric][k])
                    for r in results
                    if k in (r.get(metric) or {})
                )
                / len(results),
                4,
            )
            for k in metric_ks
        }

    # 分路 recall：{k: {路名: 平均 recall}}——回答"哪一路值得再加/删"
    route_acc: dict[str, dict[str, list[float]]] = {}
    for r in results:
        for k, per_route in (r.get("routes") or {}).items():
            for name, value in (per_route or {}).items():
                route_acc.setdefault(k, {}).setdefault(name, []).append(float(value))
    if route_acc:
        out["route_recall"] = {
            k: {name: round(sum(vals) / len(vals), 4) for name, vals in routes.items()}
            for k, routes in route_acc.items()
        }

    # 并集上限：各路结果的并集能召回多少 —— 融合策略的理论天花板。
    # 与实际 recall 的差值 = "融合还能买多少"的空间。
    # 文本可答性：有多少道题的答案**能从检索到的文本里读出来**。
    # 越低说明"必须看原图"的题越多 → 跨模态能力的缺口越大。
    answerable = [r for r in results if "text_answerable" in r]
    if answerable:
        out["text_answerable_rate"] = round(
            sum(1 for r in answerable if r.get("text_answerable")) / len(answerable), 4
        )

    # 按题型分组（"text"=文本可答 / "vision"=必须看图）：
    # 对抗题的存在意义就是让这个差值**可见**——它就是跨模态能力的缺口。
    groups: dict[str, list[dict]] = {}
    for r in results:
        kind = str(r.get("kind") or "text")
        groups.setdefault(kind, []).append(r)
    if len(groups) > 1 or "vision" in groups:
        # k 集合从逐条结果里取（aggregate 不接收 k_values 参数）
        ks: list[str] = []
        for row in groups[next(iter(groups))]:
            for key in (row.get("recall") or {}):
                if key not in ks:
                    ks.append(key)
        out["by_kind"] = {
            kind: {
                "count": len(rows),
                "recall": {
                    str(k): round(sum(float(r.get("recall", {}).get(str(k), 0.0)) for r in rows) / len(rows), 4)
                    for k in ks
                },
                "ndcg": {
                    str(k): round(sum(float(r.get("ndcg", {}).get(str(k), 0.0)) for r in rows) / len(rows), 4)
                    for k in ks
                },
            }
            for kind, rows in groups.items()
        }

    # 天花板是**候选池级**的标量（不是 @k）：各 k 的 recall 都与它比
    ceil_vals: list[float] = []
    for r in results:
        value = r.get("union_ceiling")
        if value is not None:
            ceil_vals.append(float(value))
    if ceil_vals:
        ceiling = round(sum(ceil_vals) / len(ceil_vals), 4)
        out["union_ceiling"] = ceiling
        out["fusion_headroom"] = {
            k: round(ceiling - float(out["recall"].get(k, 0.0)), 4)
            for k in out.get("recall", {})
        }
    return out


def aggregate_attribution(attributions: list[dict]) -> dict:
    """缺口归因的汇总（各环计数之和）。"""
    keys = ("not_recalled", "recalled_not_fused", "fused_not_final", "found")
    out = {k: 0 for k in keys}
    for a in attributions or []:
        for k in keys:
            out[k] += int((a or {}).get(k, 0) or 0)
    return out
