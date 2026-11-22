"""阶段模块的可独立测试面（`B0 X-27` 第二步的收益）。

拆分前这些行为只有**跑完整条流水线**才能触达（要造反排索引 + 假 LLM + 假缓存 +
假自检器），因此改一个阶段就可能连带踩坏另一个阶段；现在阶段函数是纯函数，
可以逐个钉住它们「拆分前后必须一致」的那几条语义。

这里**不重复**流水线集成测试已覆盖的端到端行为（真实性由 `tests/integration`
的 30+ 文件保证），只钉「阶段边界本身」。
"""
import types as pytypes
from datetime import datetime, timezone
import time

from app.core.envelope import ErrCode
from app.domain.schemas import RetrievalConfig, SelfCheckRecord
from app.services.generation import cache_stage, guard_stage
from app.services.generation import core as generation_core
from app.services.generation.types import is_upstream_unavailable
from app.services.llm import LLMError
from app.services.upstream import CircuitOpenError


class _Settings(pytypes.SimpleNamespace):
    resolved_provider = "deepseek"
    llm_model = "deepseek-chat"
    embedding_model = "bge-m3"
    chunk_size = 800
    temperature = 0.0


def _pip(**overrides):
    """最小 pipeline 替身：只提供阶段函数真正会读的字段。"""
    base = {
        "_settings": _Settings(),
        "_index_version": lambda: "v1",
        "_crag": None,
        "_principal_user": "",
    }
    base.update(overrides)
    return pytypes.SimpleNamespace(**base)


# ---- types.is_upstream_unavailable：只有上游故障才降级 ----


def test_circuit_open_counts_as_upstream_unavailable():
    assert is_upstream_unavailable(CircuitOpenError("llm", 30.0)) is True


def test_rate_limited_counts_as_upstream_unavailable():
    err = LLMError(ErrCode.RATE_LIMITED, "限流", retryable=True)
    assert is_upstream_unavailable(err) is True


def test_programming_error_is_not_upstream_unavailable():
    # 程序 bug / 参数错不能被降级成「稍后重试」，否则把 bug 藏起来
    assert is_upstream_unavailable(ValueError("boom")) is False
    assert is_upstream_unavailable(LLMError(ErrCode.INVALID_REQUEST, "参数错")) is False


# ---- cache_stage.cache_key：身份 / 范围 / 稳定性 ----


def test_cache_key_separates_tenant():
    cfg = RetrievalConfig()
    a = cache_stage.cache_key(_pip(), "同一个问题", "tenant-a", cfg)
    b = cache_stage.cache_key(_pip(), "同一个问题", "tenant-b", cfg)
    assert a != b


def test_cache_key_is_stable_for_same_inputs():
    cfg = RetrievalConfig()
    assert cache_stage.cache_key(_pip(), "问题", "t1", cfg) == cache_stage.cache_key(
        _pip(), "问题", "t1", cfg
    )


def test_cache_key_changes_with_principal():
    # 不同主体共享缓存会把一个人的答案喂给另一个人
    cfg = RetrievalConfig()
    a = cache_stage.cache_key(_pip(), "问题", "t1", cfg)
    b = cache_stage.cache_key(_pip(_principal_user="alice"), "问题", "t1", cfg)
    assert a != b


# ---- core.assemble：事件 → AskResult ----


def test_assemble_reads_final_event():
    events = [
        ("stage", {"name": "retrieve"}),
        ("_final", {
            "answer": "答", "evidence": [], "verdict": "answered",
            "confidence": 0.9, "followups": ["追问1"],
            "context_breakdown": {"evidence": 1}, "trace_id": "should-be-ignored",
        }),
    ]
    result = generation_core.assemble(_pip(), "trace-x", time.perf_counter(), events)
    assert result.answer == "答"
    assert result.trace_id == "trace-x"  # 以入参为准，不吃事件里的脏值
    assert result.followups == ["追问1"]
    assert result.context_breakdown == {"evidence": 1}


def test_assemble_fills_defaults_for_optional_fields():
    events = [("_final", {"answer": "答", "evidence": []})]
    result = generation_core.assemble(_pip(), "t", time.perf_counter(), events)
    assert result.verdict == "answered"
    assert result.refusal_reason == ""
    assert result.followups == []
    assert result.context_dropped == []


# ---- guard_stage.to_self_check_record：有 / 无 to_record 的两种自检器 ----


class _CheckerWithRecord:
    model_version = "judge-x"

    def to_record(self, result, answer, removed, *, passed):
        return SelfCheckRecord(
            method="llm_claim_review", checked_claims=3, removed_claims=removed,
            passed=passed, unsupported_claims=[], reasoning="ok",
            model_version=self.model_version,
        )


def test_self_check_record_uses_checker_to_record():
    checker = _CheckerWithRecord()
    record = guard_stage.to_self_check_record(checker, object(), "答案", 1, 0)
    assert record.method == "llm_claim_review"
    assert record.removed_claims == 1
    assert record.passed is True
    assert record.model_version == "judge-x"


def test_self_check_record_falls_back_without_to_record():
    class Checker:
        model_version = "judge-y"

    checker = Checker()
    result = pytypes.SimpleNamespace(
        unsupported_claims=["a"], checked_claims=2, reasoning="r"
    )
    record = guard_stage.to_self_check_record(checker, result, "答案", 0, 1)
    assert record.checked_claims == 2
    assert record.unsupported_claims == ["a"]
    assert record.passed is False  # 还有未支持的声明 → 不算通过
    assert record.model_version == "judge-y"


# ---- core.log_stage_summary：0 毫秒阶段不能被吞 ----


def test_log_stage_summary_keeps_zero_ms_stage(monkeypatch):
    captured: list[str] = []

    class _Logger:
        def info(self, fmt, *args):
            captured.append(fmt % args)

    monkeypatch.setattr(generation_core, "logger", _Logger())
    generation_core.log_stage_summary("trace-1", {"cache": 0, "llm": 1200}, 1200)
    text = captured[0]
    # 0 毫秒表示「这一段走了但没有耗时」，与「没走」是两回事
    assert "cache:0" in text
    assert "llm:1200" in text
    assert "trace-1" in text


def test_self_check_record_is_serialisable():
    """记录必须能直接进历史库（JSON 列），不能带 datetime 以外的怪类型。"""
    record = guard_stage.to_self_check_record(_CheckerWithRecord(), object(), "答", 0, 0)
    dumped = record.model_dump()
    assert isinstance(dumped["passed"], bool)
    assert isinstance(dumped["unsupported_claims"], list)
    assert datetime.now(timezone.utc).year >= 2026
