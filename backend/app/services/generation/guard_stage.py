"""自检阶段：明细记录与落库（`B0 X-27` 第二步）。

从 `services/pipeline.py` 抽出。自检**执行**仍在编排层（`core`）里按阶段调用，
这里只收两件与「记录」有关的事：把自检结果规整成落库记录、把它写进历史。

分开的理由：`to_self_check_record` 需要兼容「自检器只实现 check / regenerate」
的第三方与测试替身——这类兼容逻辑最容易随主流程改动而腐坏，独立成文件后
单测可以直接钉它，不必经过整条流水线。
"""
from ...domain.schemas import SelfCheckRecord
from ..validation.history import SELFCHECK_KIND
from ..validation.selfcheck import estimate_claims


def to_self_check_record(
    checker, result, answer: str, removed: int, remaining: int
) -> SelfCheckRecord:
    """自检明细落库记录。

    `to_record` 是可选能力：自检器替身（或第三方实现）只实现 check/regenerate 时
    不应因为缺它而把置信度降级成"未知"——那会让"做没做校验"与"校验失败"混淆。
    """
    if hasattr(checker, "to_record"):
        return checker.to_record(result, answer, removed, passed=remaining == 0)
    claims = list(getattr(result, "unsupported_claims", []) or [])
    checked = getattr(result, "checked_claims", 0) or estimate_claims(answer)
    return SelfCheckRecord(
        method="llm_claim_review",
        checked_claims=int(checked),
        removed_claims=removed,
        passed=remaining == 0,
        unsupported_claims=claims,
        reasoning=getattr(result, "reasoning", ""),
        model_version=getattr(checker, "model_version", ""),
    )


def persist_self_check(pip, trace_id: str, tenant_id: str, record) -> None:
    """把自检记录写进历史库（历史未启用时静默跳过）。"""
    if pip._history is None:
        return
    pip._history.append(SELFCHECK_KIND, {
        "trace_id": trace_id,
        "tenant_id": tenant_id,
        **record.model_dump(),
    })
