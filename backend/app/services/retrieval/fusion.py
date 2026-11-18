"""RRF 融合。

score(d) = Σ weight_i / (k + rank_i + 1)，rank 从 0 开始，k=60 为论文默认值。

只用排名不用分数：BM25（0~∞）与向量（0~1）分数分布不同，
强行归一化会引入校准难题；RRF 把"某路第 1 名"统一视为贡献度。

数值自检（上线前必须手工验算）：
- 单路排第 1 → 1/61 ≈ 0.0164；
- 两路都排第 1 → 2 × 0.0164 ≈ 0.0328，必须大于单路第 1；
- 两路同时靠前（第3+第1）胜过单路极靠前（第1+第3 颠倒亦然成立靠两边之和）。
"""
from app.services.retrieval.base import FusedCandidate

#: 注入风险块的降权系数（`KQ-27`）。0.25 ≈ 掉到同位次块的 1/4 处：
#: 明显靠后，但**不是消失**——正常内容仍可能被检索到（排除反而成了隐藏工具）。
INJECTION_RISK_PENALTY = 0.25


def rrf_fuse(
    routes: list[tuple],
    k: int = 60,
    weights: dict[str, float] | None = None,
    top_n: int | None = None,
) -> list[FusedCandidate]:
    """把多路检索结果按 RRF 融合。weights 缺省各路权重 1.0。

    routes 每项为 (route_name, candidates) 或 (route_name, candidates, origin)，
    origin 记录该路结果由哪条改写/子问题带回。
    权重按基础路名（@ 前缀之前）查找，同一基础路的多条查询共享权重。
    """
    weights = weights or {}
    fused: dict[str, FusedCandidate] = {}
    for item in routes:
        route_name, candidates = item[0], item[1]
        origin = item[2] if len(item) > 2 else None
        base_route = route_name.split("@", 1)[0]
        weight = weights.get(base_route, 1.0)
        for rank, cand in enumerate(candidates):  # rank 从 0 开始
            cid = cand.chunk.chunk_id
            entry = fused.get(cid)
            if entry is None:
                entry = FusedCandidate(chunk=cand.chunk, rrf_score=0.0, routes=[], origins=[])
                fused[cid] = entry
            entry.rrf_score += weight / (k + rank + 1)
            if base_route not in entry.routes:
                entry.routes.append(base_route)
            if origin and origin not in entry.origins:
                entry.origins.append(origin)
    results = sorted(fused.values(), key=lambda c: c.rrf_score, reverse=True)
    # `KQ-27` 第二层：命中注入风险的块**降权**（不是排除）。
    # 排除是攻击者的工具——只要在文档里写「忽略以上指令」，这段内容就被系统
    # 「保护」起来不再被检索到；降权只是让它排后面，内容仍可被合法检索到。
    #
    # **为什么要再检测一次**（而不是只读 `chunk.injection_risk`）：
    # Milvus 存储有**固定 schema**（`services/milvus_store.py` 的 `_OUTPUT_FIELDS`），
    # 入库时打的标记在 Milvus 里**不会被持久化**（改 schema 要重建集合 = 丢向量）。
    # 所以检索侧按内容再判一次——候选只有几十条，正则开销可忽略，
    # 而它保证了「无论用哪个存储后端，降权都生效」。
    flagged = [c for c in results if _is_injection_risk(c.chunk)]
    if flagged:
        for c in flagged:
            c.rrf_score *= INJECTION_RISK_PENALTY
        results.sort(key=lambda c: c.rrf_score, reverse=True)
    return results[:top_n] if top_n else results


def _is_injection_risk(chunk) -> bool:
    """该块是否命中注入风险（入库标记 **或** 按内容现判）。"""
    if getattr(chunk, "injection_risk", False):
        return True
    # 按需载入：检测模块只在需要判风险时才导入（热路径不必付导入成本）
    from app.services.injection_guard import has_injection_risk

    return has_injection_risk(getattr(chunk, "content", "") or "")
