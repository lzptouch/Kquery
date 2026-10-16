"""Reflect 节点（阶段 6 §4.4）。

三道防线（在图路由层强制执行，反射结果只是输入）：
1. 熔断：iteration ≥ max_iterations → 强制 answer（不调 LLM）；
2. 低分纠正：evidence_score < answer_threshold 却给 answer → 纠正为 continue；
3. 步骤耗尽升级：想 continue 但无下一步 → 升级为 replan。

- 生产：LLMReflector（qwen-plus 判定证据充分度）；
- 测试/mock：HeuristicReflector（按重排分与证据条数判定，确定性）。
"""
import json
import logging
import re
from typing import Protocol

from app.services.llm import LLM

logger = logging.getLogger(__name__)

ANSWER_THRESHOLD = 0.6  # 证据充分度阈值（低于此分不允许作答）

REFLECT_PROMPT = """你是证据审查员。评估当前累计证据是否足够回答用户问题。

## 用户问题
{question}

## 当前证据（共 {count} 条，检索器最高相关分 {top_score:.3f}）
{evidence_brief}

## 评分锚点（严格遵守，避免过苛或过松）
- 证据中包含与问题直接相关的定义/事实/数据 → score ≥ 0.6；
- 证据部分相关但缺关键细节 → 0.3 ~ 0.6；
- 证据与问题无关 → score < 0.3；
- 检索器相关分 ≥ 0.2 说明语料层面相关，此时 score 低于 0.3 需要充分理由。

## 输出（严格 JSON）
- score：0~1 的证据充分度；
- next_action：answer（足够作答）/ continue（继续执行下一步）/ replan（当前思路失败，重新规划）；
- reason：一句话理由。

{{"score": 0.0, "next_action": "continue", "reason": "..."}}
"""

# 检索相关度参考线：语料标定中 top_score ≥ 0.2 即为相关证据（见 config CRAG 标定口径）。
# 仅作为**参考信息**呈现给模型，不再用于抬升分数——TASK-110 修复前它会把任何弱证据
# 强行抬到 0.65（> ANSWER_THRESHOLD），使 replan 分支在生产中永不执行。
RETRIEVAL_FLOOR_SCORE = 0.2


class Reflector(Protocol):
    def reflect(self, question: str, evidence: list[dict], remaining_steps: int) -> dict: ...


def _parse_reflect(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("反思结果中未找到 JSON")
    payload = json.loads(m.group(0))
    score = float(payload.get("score", 0.0))
    score = max(0.0, min(1.0, score))
    action = str(payload.get("next_action", "continue"))
    if action not in ("answer", "continue", "replan"):
        action = "continue"
    return {"score": score, "next_action": action, "reason": str(payload.get("reason", ""))[:200]}


class LLMReflector:
    def __init__(self, llm: LLM, max_retries: int = 1):
        self._llm = llm
        self._max_retries = max_retries

    def reflect(self, question: str, evidence: list[dict], remaining_steps: int) -> dict:
        top_score = max((e.get("score", 0.0) for e in evidence), default=0.0)
        brief = "\n".join(
            f"[{i}]（相关分 {e.get('score', 0):.3f}）{e.get('content', '')[:120]}"
            for i, e in enumerate(evidence[:8])
        ) or "（暂无证据）"
        prompt = REFLECT_PROMPT.format(
            question=question, count=len(evidence), evidence_brief=brief, top_score=top_score
        )
        verdict: dict | None = None
        for _ in range(self._max_retries + 1):
            try:
                verdict = _parse_reflect(self._llm.generate(prompt))
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("反思解析失败，重试: %s", exc)
        if verdict is None:
            # 兜底：有证据就作答（保守但可用）
            return {
                "score": 0.5 if evidence else 0.0,
                "next_action": "answer" if evidence else "replan",
                "reason": "反思服务异常，按证据有无兜底",
            }
        # TASK-110：此处原本有「检索信号兜底」把 top_score≥0.2 的判定**强制抬到 0.65**
        # （> ANSWER_THRESHOLD=0.6）。由于 graph.py 随后还有对称纠正（高分+有证据 →
        # 一律改 answer），结果是：只要检索到任意一条相关度 ≥0.2 的证据，replan 分支
        # 在生产中**永不执行**，熔断率指标恒为 0，问题被彻底掩盖。
        # 改为：top_score 只作为**下限保护**——LLM 判定 answer 但给分低于阈值时不强行
        # 改判（交由 graph 的低分纠正处理）；真正的"证据够却想空转"仍由 graph 兜底。
        if verdict["score"] < ANSWER_THRESHOLD and top_score >= RETRIEVAL_FLOOR_SCORE:
            verdict["reason"] += f"（检索相关度 top={top_score:.2f} 作为参考；分数仍以 LLM 判定为准）"
        return verdict


class HeuristicReflector:
    """确定性反思：按证据条数与最高重排分判定。"""

    def __init__(self, correct_th: float = 0.14):
        self._correct_th = correct_th

    def reflect(self, question: str, evidence: list[dict], remaining_steps: int) -> dict:
        if not evidence:
            return {
                "score": 0.0,
                "next_action": "continue" if remaining_steps > 0 else "replan",
                "reason": "尚无证据",
            }
        top = max(e.get("score", 0.0) for e in evidence)
        score = min(0.95, 0.5 + top) if top >= self._correct_th else 0.3
        if top >= self._correct_th:
            return {"score": score, "next_action": "answer", "reason": f"最高分 {top:.3f} 达到阈值"}
        return {
            "score": score,
            "next_action": "continue" if remaining_steps > 0 else "replan",
            "reason": f"最高分 {top:.3f} 低于阈值",
        }
