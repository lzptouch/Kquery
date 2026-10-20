"""幻觉自检与受限重生成。

- 校验：每条事实声明是否能在已采纳证据中找到支持（qwen-plus 判定，需理由说明）；
- 重生成只一次：把无据声明显式列给 LLM 强制删除或改写；二次校验仍有
  无据声明 → 如实输出并下调置信度，不允许掩饰；
- 置信度折算：confidence = max(0, 1 - 0.2 × 无据声明条数)。
  只反映"答案与证据的一致性"，不是概率，不反映证据本身是否正确；
- 自检器故障 → 降级：不阻断答案返回，置信度标为 None（未知）并如实提示。
"""
import logging

from pydantic import BaseModel, Field

from app.domain.schemas import SelfCheckRecord
from app.services.llm import LLM
from app.services.query.jsonutil import JsonParseError, parse_json_model

logger = logging.getLogger(__name__)

SELFCHECK_PROMPT = """你是事实校验器。逐句核对答案中的事实性声明是否能在证据中找到支持。

证据：
{evidence_block}

答案：
{answer}

只输出 JSON：{{"is_hallucination": true/false, "checked_claims": 送检的事实性声明条数, "unsupported_claims": ["无据声明1", ...], "reasoning": "一句话结论"}}"""

REGENERATE_PROMPT = """你是知识库问答助手。仅根据下面给出的证据片段回答用户问题，不允许使用证据之外的知识。
上次生成的答案包含以下**无据声明**，本次必须删除或改写它们：{claims}

证据片段：
{evidence_block}
---
用户问题：{question}

回答要求：
1. 答案中的每个事实性陈述后标注引用编号，如 [#1]；
2. 严禁出现上述无据声明及其变体；
3. 答案末尾列出引用片段编号；
4. 证据不足时直接回答"知识库无相关内容"。"""


class SelfCheckResult(BaseModel):
    is_hallucination: bool = False
    checked_claims: int = 0  # 模型自报的送检声明条数（0 = 模型漏报，退回本地估算）
    unsupported_claims: list[str] = Field(default_factory=list)
    reasoning: str = ""

    def effective_checked_claims(self, answer: str) -> int:
        """模型漏报条数时用本地启发式兜底估算（句子数下限为 1）。"""
        if self.checked_claims > 0:
            return self.checked_claims
        return max(1, estimate_claims(answer))


# 事实性陈述的最小判定：中文断句计数。模型漏报 checked_claims 时兜底用。
_CLAIM_SEPARATORS = "。！？!?；;\n"


def estimate_claims(answer: str) -> int:
    """粗略估计事实性声明条数（仅在模型未给出 checked_claims 时兜底）。

    句末标点不额外计一条：否则"一句话。"会被算成 2 句，忠实度分母被系统性高估。
    """
    text = (answer or "").strip()
    if not text:
        return 0
    breaks = sum(text.count(ch) for ch in _CLAIM_SEPARATORS)
    # 结尾的标点不开启新句
    if text[-1] in _CLAIM_SEPARATORS:
        breaks -= 1
    return max(1, breaks + 1)


def compute_confidence(unsupported_count: int) -> float:
    """截断线性折减：0 条 → 1.0；1 条 → 0.8；3 条 → 0.4；5 条及以上 → 0。"""
    return max(0.0, 1.0 - 0.2 * unsupported_count)


class HallucinationChecker:
    def __init__(self, llm: LLM, max_retries: int = 1, model_version: str = ""):
        self._llm = llm
        self._max_retries = max_retries
        self.model_version = model_version

    def check(self, answer: str, evidence_block: str) -> SelfCheckResult:
        """失败抛 JsonParseError/LLMError，由上层降级（不阻断答案返回）。"""
        prompt = SELFCHECK_PROMPT.format(evidence_block=evidence_block, answer=answer)
        last_error: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                raw = self._llm.generate(prompt)
                return parse_json_model(raw, SelfCheckResult)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("幻觉自检重试: %s", exc)
        raise JsonParseError(f"幻觉自检失败: {last_error}")

    def to_record(self, result: SelfCheckResult, answer: str, removed: int, passed: bool) -> SelfCheckRecord:
        """转成可落库 / 可回传的 SELF_CHECK 实体（）。"""
        return SelfCheckRecord(
            method="llm_claim_review",
            checked_claims=result.effective_checked_claims(answer),
            removed_claims=removed,
            passed=passed,
            unsupported_claims=list(result.unsupported_claims),
            reasoning=result.reasoning,
            model_version=self.model_version,
        )

    def regenerate(self, question: str, evidence_block: str, claims: list[str]) -> str:
        """受限重生成：显式列出无据声明，强制删除或改写。只重生成一次。"""
        prompt = REGENERATE_PROMPT.format(
            claims="、".join(claims),
            evidence_block=evidence_block,
            question=question,
        )
        return self._llm.generate(prompt)
