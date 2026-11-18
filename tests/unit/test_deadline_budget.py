"""请求级 deadline 与超时预算链的验证测试（X-05 / X-20）。

对应改造：
- 线性问答路径此前**没有任何请求级上限**：单次 LLM 超时 60s × 3 次尝试 + 阻塞退避
  ≈183s，网关先超时返回 502，服务端还在烧 token。
- 现在：请求带 `Deadline`（剩余额度）→ 每次上游调用的超时按剩余额度裁剪，
  重试次数随剩余额度递减，退避等待也受预算约束。
"""
import sys
import time
from types import SimpleNamespace

import pytest

from app.core.deadline import Deadline, supports_deadline
from app.services.llm import LLMError, QwenLLM


class TestDeadline:
    def test_remaining_counts_down(self):
        d = Deadline(1000)
        assert 0 < d.remaining_ms <= 1000
        time.sleep(0.02)
        assert d.remaining_ms < 1000
        assert d.elapsed_ms >= 20

    def test_zero_budget_is_expired(self):
        assert Deadline(0).expired is True
        assert Deadline(0).remaining_ms == 0

    def test_expired_after_budget(self):
        now = {"t": 0.0}
        d = Deadline(1000, clock=lambda: now["t"])
        assert d.expired is False
        now["t"] = 1.5
        assert d.elapsed_ms == 1500
        assert d.expired is True

    def test_can_wait_includes_reserve(self):
        """额度要够"等 + 再跑一会儿"，否则这次重试注定超时。"""
        d = Deadline(1000)
        assert d.can_wait(0.2, reserve_s=0.5) is True
        assert d.can_wait(0.8, reserve_s=0.5) is False

    def test_wait_s_refuses_when_budget_insufficient(self):
        d = Deadline(100)
        assert d.wait_s(1.0) is False
        assert d.wait_s(0.0) is True

    def test_slice_ms_respects_cap(self):
        d = Deadline(1000)
        assert d.slice_ms(0.5) <= 500
        assert d.slice_ms(1.0, cap_ms=100) == 100


class TestSupportsDeadline:
    def test_explicit_parameter(self):
        def f(prompt, deadline=None):
            return prompt

        assert supports_deadline(f) is True

    def test_var_keyword(self):
        def f(prompt, **kwargs):
            return prompt

        assert supports_deadline(f) is True

    def test_plain_function(self):
        def f(prompt):
            return prompt

        assert supports_deadline(f) is False


class _FakeGeneration:
    """假的 dashscope：记录调用参数并总是失败。"""

    calls: list = []

    @classmethod
    def reset(cls):
        cls.calls = []

    @classmethod
    def call(cls, **kwargs):
        cls.calls.append(kwargs)
        raise RuntimeError("上游炸了")


@pytest.fixture()
def fake_dashscope(monkeypatch):
    _FakeGeneration.reset()
    monkeypatch.setitem(sys.modules, "dashscope", SimpleNamespace(Generation=_FakeGeneration))
    return _FakeGeneration


class TestLlmRetryBudget:
    def test_no_retry_when_budget_insufficient(self, fake_dashscope):
        """剩余额度不够"等 1s 再试一次" → 只试一次，不做注定超时的重试。"""
        llm = QwenLLM(api_key="k", max_retries=3, timeout_s=60)
        with pytest.raises(LLMError):
            list(llm.stream("p", deadline=Deadline(1200)))
        assert len(fake_dashscope.calls) == 1

    def test_retry_allowed_when_budget_allows(self, fake_dashscope):
        llm = QwenLLM(api_key="k", max_retries=2, timeout_s=60)
        with pytest.raises(LLMError):
            list(llm.stream("p", deadline=Deadline(60000)))
        assert len(fake_dashscope.calls) == 2

    def test_attempt_timeout_clipped_by_budget(self, fake_dashscope):
        """单次调用的超时取"配置上限"与"剩余额度"的较小者。"""
        llm = QwenLLM(api_key="k", max_retries=1, timeout_s=60)
        with pytest.raises(LLMError):
            list(llm.stream("p", deadline=Deadline(2000)))
        assert fake_dashscope.calls[0]["timeout"] <= 2.0

    def test_without_deadline_behaviour_unchanged(self, fake_dashscope):
        """不传 deadline = 旧行为（重试次数不变），6 处老调用方零影响。"""
        llm = QwenLLM(api_key="k", max_retries=2, timeout_s=60)
        with pytest.raises(LLMError):
            list(llm.stream("p"))
        assert len(fake_dashscope.calls) == 2


class TestPipelineDeadlinePlumbing:
    """线性路径的 deadline 必须一路传到 LLM；不支持的实现要能退化。"""

    @staticmethod
    def _pipeline(llm):
        from app.services.pipeline import RagPipeline

        p = object.__new__(RagPipeline)
        p._llm = llm
        return p

    def test_deadline_is_passed(self):
        seen = {}

        class Conforming:
            def generate(self, prompt, deadline=None):
                seen["deadline"] = deadline
                return "ok"

        d = Deadline(5000)
        out = self._pipeline(Conforming())._llm_generate("q", deadline=d)
        assert out == "ok"
        assert seen["deadline"] is d

    def test_non_conforming_degrades(self):
        """不支持 deadline 的实现（测试替身/第三方适配器）不得让请求失败。"""

        class Legacy:
            def generate(self, prompt):
                return "ok"

        out = self._pipeline(Legacy())._llm_generate("q", deadline=Deadline(5000))
        assert out == "ok"

    def test_no_deadline_no_extra_kwarg(self):
        seen = {}

        class Conforming:
            def generate(self, prompt, deadline=None):
                seen["deadline"] = deadline
                return "ok"

        self._pipeline(Conforming())._llm_generate("q")
        assert seen["deadline"] is None
