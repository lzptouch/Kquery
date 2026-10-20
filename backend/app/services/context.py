"""上下文组装治理（ / ）。

此前的组装方式有三个问题：

1. **无 token 预算**：只按条数（`top_k`）控制。条数相同不代表长度相同——
   一段 3000 字的表格块和一段 50 字的短句都算"一条"，前者能直接撑爆上下文。
2. **父块重复**：父块回查把多个命中子块替换成同一个父块全文，替换后**不重排不截断**
   （`pipeline.py` 的父块展开），同一段长文可能进提示词 N 次。
3. **无多样性约束**：Top-K 全来自同一篇文档的相邻段落时，模型看到的是同一件事的
   五种说法，覆盖面反而不如"两篇不同文档各两条"。

本模块提供一条四段式流水线：**去重 → MMR 挑选 → token 预算裁剪 → 硬截断**。
按顺序执行，每段都可能把候选数降下来。
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 中文为主的语料按"约 1.5 字符/token"折算；英文约 4 字符/token。
# 取 2.0 是**保守估计**（宁可低估容量、提前裁剪，也不要算完发现超限被上游截断）。
CHARS_PER_TOKEN = 2.0


def estimate_tokens(text: str) -> int:
    """字符数 → token 数的粗略折算（用于预算判定，不用于计费）。"""
    return max(1, int(len(text or "") / CHARS_PER_TOKEN))


@dataclass
class ContextItem:
    """进入上下文的最小单元。"""

    key: str          # 唯一标识（chunk_id 或 parent_id）
    doc_id: str
    content: str
    score: float = 0.0
    source: str = ""
    rank: int = 0
    reason: str = ""  # 被淘汰/被截断的原因（回传给前端解释"为什么没进上下文"）


@dataclass
class ContextResult:
    items: list[ContextItem] = field(default_factory=list)
    dropped: list[ContextItem] = field(default_factory=list)
    tokens: int = 0
    truncated: bool = False


def assemble_context(
    candidates: list[ContextItem],
    token_budget: int = 6000,
    mmr_lambda: float = 0.7,
    mmr_pool_factor: int = 3,
    max_items: int = 0,
    dedupe_keys: bool = True,
) -> ContextResult:
    """把候选证据组装成"能塞进预算、且尽量不重复"的上下文。

    参数：
    - `token_budget`：上下文 token 上限（0 表示不限制）
    - `mmr_lambda`：MMR 的相关性权重（1.0 = 纯按分数，0.0 = 纯多样性）
    - `mmr_pool_factor`：在 `max_items × factor` 的候选池里做 MMR（池子太小选不出多样性）
    - `max_items`：条数上限（0 表示只由 token 预算控制）
    - `dedupe_keys`：按 key 去重（父块回查后同一 key 只保留一次）
    """
    result = ContextResult()
    if not candidates:
        return result

    # ---- ① 去重（父块回查后同一 parent_id 会重复出现）----
    pool: list[ContextItem] = []
    if dedupe_keys:
        seen: set[str] = set()
        for c in candidates:
            if c.key in seen:
                dup = ContextItem(**{**c.__dict__, "reason": "重复块（父块展开/多路命中）"})
                result.dropped.append(dup)
                continue
            seen.add(c.key)
            pool.append(c)
    else:
        pool = list(candidates)

    # ---- ② MMR 挑选（限相关性与多样性）----
    limit = max_items if max_items > 0 else len(pool)
    if mmr_lambda >= 1.0 or len(pool) <= 1:
        selected = sorted(pool, key=lambda c: c.score, reverse=True)[:limit]
    else:
        selected = _mmr_select(pool, limit, mmr_lambda, mmr_pool_factor)
    not_selected = [c for c in pool if c not in selected]
    for c in not_selected:
        result.dropped.append(
            ContextItem(**{**c.__dict__, "reason": "多样性挑选未入选（MMR）"})
        )

    # ---- ③ token 预算裁剪（低分先出局）----
    selected.sort(key=lambda c: c.score, reverse=True)
    kept: list[ContextItem] = []
    used = 0
    for c in selected:
        cost = estimate_tokens(c.content)
        if token_budget and used + cost > token_budget:
            result.dropped.append(
                ContextItem(**{**c.__dict__, "reason": f"超出 token 预算（{token_budget}）"})
            )
            continue
        kept.append(c)
        used += cost

    # ---- ③.5 保底：一条都没留下时，保留最高分那条的**截断版** ----
    # 只有一条候选且它本身就超预算时（长表格块很常见），③ 会把它整条丢掉，
    # 结果是上下文为空、答案完全没有证据——这比"截断版证据"更糟。
    if not kept and selected and token_budget:
        top = selected[0]
        keep_chars = max(1, int(token_budget * CHARS_PER_TOKEN))
        result.dropped = [d for d in result.dropped if d.key != top.key]
        kept = [ContextItem(
            **{**top.__dict__, "content": top.content[:keep_chars],
               "reason": "超预算，保留截断版（否则上下文为空）"}
        )]
        used = estimate_tokens(kept[0].content)
        result.truncated = True

    # ---- ④ 硬截断（单条就超预算时的兜底）----
    if token_budget and used > token_budget and kept:
        keep_chars = int(token_budget * CHARS_PER_TOKEN)
        joined = sum(len(c.content) for c in kept)
        if joined > keep_chars:
            logger.warning(
                "上下文硬截断：%d 字 → %d 字（预算 %d token）", joined, keep_chars, token_budget
            )
            # 按顺序填满预算，最后一条切一半
            trimmed: list[ContextItem] = []
            remaining = keep_chars
            for c in kept:
                if remaining <= 0:
                    result.dropped.append(
                        ContextItem(**{**c.__dict__, "reason": "硬截断丢弃"})
                    )
                    continue
                if len(c.content) <= remaining:
                    trimmed.append(c)
                    remaining -= len(c.content)
                else:
                    cut = ContextItem(**{**c.__dict__, "content": c.content[:remaining],
                                         "reason": "硬截断（保留前段）"})
                    trimmed.append(cut)
                    remaining = 0
            kept = trimmed
            result.truncated = True
            used = sum(estimate_tokens(c.content) for c in kept)

    # 按 rank 回正序输出，保持与引用编号一致
    kept.sort(key=lambda c: c.rank)
    result.items = kept
    result.tokens = used
    return result


def _mmr_select(
    pool: list[ContextItem], limit: int, lam: float, pool_factor: int
) -> list[ContextItem]:
    """最大边际相关性选择（贪心）。

    相似度用**字符 n-gram 的 Jaccard** 近似：embedding 相似度要额外一次模型调用，
    而这里的目的是"别让同一段话说五遍"，词面重合度已经足够——且零成本、无外部依赖。
    """
    if limit <= 0:
        return []
    ordered = sorted(pool, key=lambda c: c.score, reverse=True)
    # 候选池不需要比 limit 大太多：池子越大越慢，收益递减
    ordered = ordered[: max(limit, limit * max(1, pool_factor))]
    if not ordered:
        return []

    selected = [ordered[0]]
    remaining = ordered[1:]
    while remaining and len(selected) < limit:
        best_item, best_val = None, float("-inf")
        for cand in remaining:
            sim = max(_jaccard(cand.content, s.content) for s in selected)
            val = lam * cand.score - (1 - lam) * sim
            if val > best_val:
                best_val, best_item = val, cand
        if best_item is None:
            break
        selected.append(best_item)
        remaining.remove(best_item)
    return selected


def _ngrams(text: str, n: int = 4) -> set[str]:
    t = (text or "").strip()
    if len(t) <= n:
        return {t} if t else set()
    return {t[i : i + n] for i in range(len(t) - n + 1)}


def _jaccard(a: str, b: str) -> float:
    sa, sb = _ngrams(a), _ngrams(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0
