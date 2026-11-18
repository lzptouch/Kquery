"""历史摘要压缩（`ST-02`）：更早的轮次压成结构化摘要。

## 问题

跨轮历史此前**只有条数裁剪，没有内容压缩**：每轮都把最近 N 轮的问答原文
塞进提示词，于是 token 成本随轮数**平方**增长（第 10 轮比第 1 轮贵得多，
而用户往往只是在追问同一个话题）。

`services/agent/sessions.py` 的 docstring 曾写着「摘要压缩默认关闭」——
那是**有契约无实现**的残留承诺；本模块把承诺变成真的。

## 三条纪律

1. **压缩本身是一次模型调用** → **预算闸门前置**：预算不够时直接走
   `truncate_fallback`，**不为了省 token 先把预算烧穿**（压缩没跑成反而多花一次调用，
   是本末倒置）。
2. **模型不可用时回落截断，绝不抛错打断问答**——压缩是优化，不是必需路径。
3. **摘要用纯文本，不用 JSON**：摘要本身是省 token 的动作，引入 JSON 解析失败面
   得不偿失（解析失败 → 又要一套兜底）。

## 输出契约

`CompactionResult` 给出：摘要正文、保留原文的最近轮次、被摘要掉的轮次数、
压缩前后估算 token、压缩方式（`llm` / `truncate_fallback` / `skipped`）。
摘要与原文都带**来源标记**（保留的轮次带原问题摘要），可回溯。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 摘要提示词（固定四段，纯文本输出）
SUMMARY_PROMPT = """请把下面这段多轮对话压缩成一份结构化摘要，供后续问答参考。

按以下四段输出，每段一行，不要多余解释：
意图：用户这段时间想解决什么
已确认事实：已经明确成立的结论（逐条列出）
未解决问题：仍未解决、需要后续澄清的点（逐条列出）
用户偏好：用户表达方式、关注点、格式要求等

对话记录：
{history}

要求：只保留对话里出现过的信息，不要补充外部知识；不确定的写「未确认」而不是猜。"""

#: 压缩方式
METHOD_LLM = "llm"
METHOD_TRUNCATE = "truncate_fallback"
METHOD_SKIPPED = "skipped"


def estimate_history_tokens(turns: list[dict]) -> int:
    """估算一轮历史占多少 token（字符数 / 2，粗算；用于判定是否值得压缩）。"""
    total = 0
    for turn in turns or []:
        total += len(str(turn.get("question") or ""))
        total += len(str(turn.get("answer") or ""))
    return max(1, total // 2)


@dataclass
class CompactionResult:
    """一次压缩的结果。"""

    summary: str = ""
    recent_turns: list[dict] = field(default_factory=list)
    compacted_turns: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    method: str = METHOD_SKIPPED
    note: str = ""

    @property
    def ratio(self) -> float:
        """压缩后 / 压缩前的 token 比（≤ 0.5 才算「压住了」）。"""
        if self.tokens_before <= 0:
            return 1.0
        return round(self.tokens_after / self.tokens_before, 4)

    def as_dict(self) -> dict:
        return {
            "summary": self.summary,
            "compacted_turns": self.compacted_turns,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "ratio": self.ratio,
            "method": self.method,
            "note": self.note,
        }


def truncate_fallback(
    turns: list[dict],
    keep_recent: int,
    tokens_before: int = 0,
) -> CompactionResult:
    """截断回落：保留**首轮意图** + 最近 N 轮，其余丢弃。

    为什么保留首轮：用户的原始问题决定了整段对话的目标；
    只留最近几轮会丢掉「为什么问这个」。
    """
    turns = list(turns or [])
    before = tokens_before or estimate_history_tokens(turns)
    if not turns:
        return CompactionResult(tokens_before=before, tokens_after=0, method=METHOD_TRUNCATE,
                                note="没有历史可压缩")
    keep = max(1, int(keep_recent))
    if len(turns) <= keep:
        return CompactionResult(
            summary="",
            recent_turns=turns,
            compacted_turns=0,
            tokens_before=before,
            tokens_after=before,
            method=METHOD_SKIPPED,
            note="历史轮次未超过保留数，无需压缩",
        )
    head = turns[0]
    recent = turns[-keep:]
    dropped = len(turns) - len(recent)
    first_intent = str(head.get("question") or "").strip()
    summary = f"意图：{first_intent}\n已确认事实：（历史已按保留策略截断，未做模型摘要）"
    after = estimate_history_tokens(recent) + max(1, len(summary) // 2)
    return CompactionResult(
        summary=summary,
        recent_turns=recent,
        compacted_turns=dropped,
        tokens_before=before,
        tokens_after=after,
        method=METHOD_TRUNCATE,
        note=f"模型摘要不可用，按截断回落（保留首轮意图 + 最近 {keep} 轮）",
    )


def compact_history(
    turns: list[dict],
    llm=None,
    *,
    keep_recent: int = 3,
    trigger_tokens: int = 4000,
    max_output_tokens: int = 600,
    budget_ok: bool = True,
) -> CompactionResult:
    """压缩历史（`ST-02`）。

    - `turns`：轮次列表（`{"question", "answer"}`）；
    - `llm`：模型客户端（可为 None → 直接截断回落）；
    - `keep_recent`：保留原文的最近轮数；
    - `trigger_tokens`：超过该 token 数才触发压缩；
    - `budget_ok`：预算闸门（False → **不发起模型调用**，直接截断回落）。
    """
    turns = list(turns or [])
    before = estimate_history_tokens(turns)
    if before < trigger_tokens:
        return CompactionResult(
            recent_turns=turns,
            tokens_before=before,
            tokens_after=before,
            method=METHOD_SKIPPED,
            note=f"历史 {before} token < 触发阈值 {trigger_tokens}，不压缩",
        )

    keep = max(1, int(keep_recent))
    if len(turns) <= keep:
        return CompactionResult(
            recent_turns=turns,
            tokens_before=before,
            tokens_after=before,
            method=METHOD_SKIPPED,
            note=f"轮次数 {len(turns)} ≤ 保留数 {keep}，无需压缩",
        )

    # 纪律 1：预算闸门前置——预算不够就不发起压缩调用
    if not budget_ok:
        return truncate_fallback(turns, keep, tokens_before=before)

    older = turns[:-keep]
    recent = turns[-keep:]
    history_text = "\n".join(
        f"第 {i + 1} 轮\n问：{t.get('question', '')}\n答：{t.get('answer', '')}"
        for i, t in enumerate(older)
    )
    prompt = SUMMARY_PROMPT.format(history=history_text)
    try:
        summary = llm.generate(prompt, max_tokens=max_output_tokens)
    except TypeError:
        # 适配器不支持 max_tokens（第三方实现 / 测试替身）
        try:
            summary = llm.generate(prompt)
        except Exception as exc:  # noqa: BLE001 - 压缩失败不打断问答
            logger.warning("历史摘要失败，回落截断: %s", exc)
            return truncate_fallback(turns, keep, tokens_before=before)
    except Exception as exc:  # noqa: BLE001 - 压缩失败不打断问答
        logger.warning("历史摘要失败，回落截断: %s", exc)
        return truncate_fallback(turns, keep, tokens_before=before)

    summary = str(summary or "").strip()
    if not summary:
        # 空摘要 = 没压到任何东西 → 回落截断（比带着空摘要继续走更诚实）
        return truncate_fallback(turns, keep, tokens_before=before)

    after = estimate_history_tokens(recent) + max(1, len(summary) // 2)
    return CompactionResult(
        summary=summary,
        recent_turns=recent,
        compacted_turns=len(older),
        tokens_before=before,
        tokens_after=after,
        method=METHOD_LLM,
        note=f"模型摘要覆盖 {len(older)} 轮，保留最近 {keep} 轮原文",
    )
