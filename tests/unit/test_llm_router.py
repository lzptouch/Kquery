"""模型路由与降级（`LM-01`）。

## 六条验收

① 主模型成功 → 不降级
② 主模型超时（可重试）→ 落到备选、降级标记为真、原因为超时
③ 401（不可重试）→ **不降级**，直接抛
④ **熔断 → 不降级**，直接上抛
⑤ 降级计数 +1
⑥ 全部候选失败 → 抛原异常（不吞）
"""
from __future__ import annotations

import pytest

from app.core.envelope import ErrCode
from app.services.llm import LLMError
from app.services.llm_router import (
    REASON_RATE_LIMITED,
    REASON_SERVER_ERROR,
    REASON_TIMEOUT,
    LLMRouter,
    ModelPolicy,
)


class _FakeLLM:
    """按模型名决定行为的假模型。"""

    def __init__(self, model: str, behavior: str = "ok", text: str = "答案") -> None:
        self.model = model
        self.behavior = behavior
        self.text = text
        self.last_usage = None

    def generate(self, prompt, deadline=None):
        if self.behavior == "ok":
            return self.text
        if self.behavior == "timeout":
            raise LLMError(ErrCode.GENERATION_TIMEOUT, "超时", retryable=True)
        if self.behavior == "auth":
            raise LLMError(
                ErrCode.GENERATION_FAILED, "鉴权失败", retryable=False, degradable=False
            )
        if self.behavior == "circuit":
            raise LLMError(
                ErrCode.DEPENDENCY_UNAVAILABLE, "熔断", retryable=False, degradable=False
            )
        if self.behavior == "server":
            raise LLMError(ErrCode.GENERATION_FAILED, "5xx", retryable=True)
        raise AssertionError(self.behavior)

    def stream(self, prompt, deadline=None):
        yield self.generate(prompt, deadline=deadline)


def _router(behavior: str, on_degrade=None, fallbacks=("fast-model",)) -> LLMRouter:
    def factory(model: str):
        return _FakeLLM(model, behavior=behavior if model == "main-model" else "ok")

    policy = ModelPolicy(primary="main-model", fallbacks=tuple(fallbacks))
    return LLMRouter(factory, policy, on_degrade=on_degrade)


def test_primary_success_is_not_degraded():
    """① 主模型成功 → 不降级。"""
    router = _router("ok")
    assert router.generate("问题") == "答案"
    r = router.last_result
    assert r.model == "main-model"
    assert r.degraded is False
    assert r.reason == ""


def test_timeout_falls_back_to_backup_and_marks_degraded():
    """② 超时（可重试）→ 落到备选、降级标记为真、原因为超时。"""
    router = _router("timeout")
    router.generate("问题")
    r = router.last_result
    assert r.model == "fast-model"
    assert r.degraded is True
    assert r.reason == REASON_TIMEOUT


def test_auth_error_does_not_degrade():
    """③ 401（不可重试）→ **不降级**，直接抛。"""
    with pytest.raises(LLMError) as excinfo:
        _router("auth").generate("问题")
    assert int(excinfo.value.code) == int(ErrCode.GENERATION_FAILED)


def test_circuit_open_does_not_degrade():
    """④ 熔断 → 不降级，直接上抛（换同平台模型无济于事）。"""
    with pytest.raises(LLMError) as excinfo:
        _router("circuit").generate("问题")
    assert int(excinfo.value.code) == int(ErrCode.DEPENDENCY_UNAVAILABLE)


def test_degrade_counter_increments_once():
    """⑤ 降级计数 +1（低基数原因标签）。"""
    seen: list[str] = []
    _router("timeout", on_degrade=seen.append).generate("问题")
    assert seen == [REASON_TIMEOUT]


def test_rate_limit_reason_is_distinguished():
    """限流与超时是不同的降级原因（看板要能分开看）。"""
    class _RL(_FakeLLM):
        def generate(self, prompt, deadline=None):
            raise LLMError(ErrCode.RATE_LIMITED, "限流", retryable=True)

    def factory(model: str):
        # 主模型限流；备选正常（否则测的是「全部候选失败」）
        return _RL(model, "rate_limited") if model == "main-model" else _FakeLLM(model, "ok")

    seen: list[str] = []
    router = LLMRouter(factory, ModelPolicy("main-model", ("fast",)), seen.append)
    router.generate("问题")
    r = router.last_result
    assert r.degraded is True
    assert r.reason == REASON_RATE_LIMITED
    assert seen == [REASON_RATE_LIMITED]


def test_all_candidates_fail_raises_original_error():
    """⑥ 全部候选失败 → 抛原异常（不吞、不假装成功）。"""
    def factory(model: str):
        return _FakeLLM(model, behavior="server")

    router = LLMRouter(factory, ModelPolicy("main-model", ("fast",)))
    with pytest.raises(LLMError) as excinfo:
        router.generate("问题")
    assert int(excinfo.value.code) == int(ErrCode.GENERATION_FAILED)


def test_reason_for_server_error():
    """5xx → 归到 server_error（不是 timeout / rate_limited）。"""
    router = _router("server")
    router.generate("问题")
    r = router.last_result
    assert r.degraded is True
    assert r.reason == REASON_SERVER_ERROR


def test_max_attempts_limits_candidates():
    """候选链受 max_attempts 限制（不会无限试下去）。"""
    policy = ModelPolicy(primary="a", fallbacks=("b", "c", "d"), max_attempts=2)
    assert policy.candidates() == ["a", "b"]
