"""流式收尾（`C-13` 方案 A）与闲聊分支的 `_final` 契约。

## 这两条用例钉的是什么

**D-3（死参数 `cancel`）**：`_finalize_streamed_answer(..., cancel=...)` 声明了
`cancel` 却从不读它。流式收尾里唯一的昂贵动作是**自检**（额外一次模型调用），
而客户端的断开探测（`C-04`）本来已经知道人走了。修法是：断开 → 跳过自检，
但必须**如实**把 `unvalidated` 置真，不能让记录看起来「校验过」。

**D-4（闲聊 `reranked` 硬编码）**：闲聊分支在检索之前，既没检索也就谈不上
「精排 / 未精排」。此前硬编码 `"reranked": True`，等于对外声明「已精排」。
现在**不输出该字段**（前端只在 `=== false` 时提示降级，省略即不提示）。

## 为什么用假 self 而不是跑整条管线

这两处逻辑是纯函数式的收尾分支，不依赖检索/索引/向量库。构造真的 `Pipeline`
要把后面十几个协作者全部装配起来，测试会变得又慢又脆，却测不到更多东西。
用 `Pipeline._finalize_streamed_answer.__get__(fake)` 绑定一个只带所需属性的对象，
既锁住行为，也把「这个方法到底依赖哪些属性」变成显式契约。
"""
import threading
from types import SimpleNamespace

import pytest

from app.services.generation import core as generation_core
from app.services.pipeline import RagPipeline as Pipeline


class _Checker:
    """自检器替身：被调用即记录（用来证明"跳过"是跳过）。"""

    def __init__(self) -> None:
        self.calls = 0

    def check(self, answer, evidence_block):  # noqa: ANN001, ANN201 - 替身
        self.calls += 1
        return SimpleNamespace(unsupported_claims=[], score=1.0)


class _Prom:
    """指标替身：只实现被用到的 `shed_total.labels(...).inc()`。"""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    @property
    def shed_total(self):  # noqa: ANN201 - 替身
        outer = self

        class _Counter:
            def labels(self, **kw):  # noqa: ANN003, ANN202 - 替身
                outer.reasons.append(kw.get("reason", ""))
                return SimpleNamespace(inc=lambda: None)

        return _Counter()

    def record_error(self, code) -> None:  # noqa: ANN001 - 替身
        self.reasons.append(f"error:{code}")


class _Persist:
    def __init__(self) -> None:
        self.records: list = []

    def __call__(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202 - 替身
        self.records.append(args)


def _fake_self(checker: _Checker, prom: _Prom, persist: _Persist) -> object:
    holder = SimpleNamespace(
        _self_checker=checker,
        _settings=SimpleNamespace(selfcheck_enabled=True),
        _prom=prom,
        _persist_self_check=persist,
        _llm=None,
        _context_dropped=[],
        _context_breakdown={},
        _generate_followups=lambda answer: [],
        # 自检成功路径会构造一条记录并落盘：给个最小替身，
        # 这样用例才能走到「自检成功」而不是被 except 兜住
        _to_self_check_record=lambda *a, **k: SimpleNamespace(
            elapsed_ms=0, model_dump=lambda: {}
        ),
    )
    # 只绑定一个方法：不构造 Pipeline（那要装十几个协作者）
    return SimpleNamespace(
        **vars(holder),
        _finalize_streamed_answer=Pipeline._finalize_streamed_answer.__get__(holder),
    )


def _run(fake, cancel=None, evidence=None):  # noqa: ANN001, ANN202 - 测试辅助
    return list(
        fake._finalize_streamed_answer(
            "问题", "default", "trace-1",
            {"intent": "qa"}, None, None,
            "答案正文", [{"x": 1}], "证据块",
            0, {}, cancel, evidence=evidence,
        )
    )


class TestCancelSkipsSelfCheck:
    def test_client_gone_skips_selfcheck_and_marks_unvalidated(self):
        checker, prom, persist = _Checker(), _Prom(), _Persist()
        cancel = threading.Event()
        cancel.set()  # 客户端已断开

        events = _run(_fake_self(checker, prom, persist), cancel=cancel)

        assert checker.calls == 0, "客户端已断开仍调用了自检（白花一次模型调用）"
        assert "cancel_selfcheck" in prom.reasons
        confidence = dict(events)["confidence"]
        assert confidence["unvalidated"] is True, "跳过自检却标成已校验 = 记录不可信"
        assert confidence["score"] is None
        assert persist.records == []

    def test_selfcheck_still_runs_when_client_alive(self):
        """未断开时行为不变（这条防止"顺手把自检删了"）。"""
        checker, prom, persist = _Checker(), _Prom(), _Persist()
        cancel = threading.Event()  # 未 set

        events = _run(_fake_self(checker, prom, persist), cancel=cancel)

        assert checker.calls == 1
        assert "cancel_selfcheck" not in prom.reasons
        assert dict(events)["confidence"]["unvalidated"] is False

    def test_cancel_none_is_the_old_behaviour(self):
        """不传 cancel（阻塞路径/旧调用面）→ 照常自检。"""
        checker, prom, persist = _Checker(), _Prom(), _Persist()

        events = _run(_fake_self(checker, prom, persist), cancel=None)

        assert checker.calls == 1
        assert dict(events)["confidence"]["unvalidated"] is False


class TestFinalContract:
    def test_streamed_final_never_claims_regeneration(self):
        checker, prom, persist = _Checker(), _Prom(), _Persist()

        events = _run(_fake_self(checker, prom, persist))
        final = dict(events)["_final"]

        assert final["regenerated"] is False
        assert final["streamed_no_regen"] is True

    def test_no_retrieval_means_not_reranked(self):
        """无检索结果（`hybrid_result is None`）→ `reranked` 必须是 False。

        此前是 `... if hybrid_result else True`：既没检索又宣称"已精排"，
        前端据此不显示「未精排」提示，等于把一次能力缺失藏起来。
        """
        checker, prom, persist = _Checker(), _Prom(), _Persist()

        events = _run(_fake_self(checker, prom, persist))
        assert dict(events)["_final"]["reranked"] is False


class TestChitchatFinal:
    """闲聊分支：不声明 `reranked`（没检索就没有"精排"这件事）。"""

    @pytest.mark.parametrize("forbidden", ["reranked"])
    def test_chitchat_final_omits_reranked(self, forbidden):
        import inspect

        # `B0 X-27` 第二步后编排在 `generation/core.py`，源码断言跟到新位置
        src = inspect.getsource(generation_core.core_events)
        # 闲聊分支的 _final 就在 `_llm_generate_or_degrade` 之后那一小段
        idx = src.find('"用户在闲聊')
        assert idx > 0, "没找到闲聊分支（用例已过期，请重新定位）"
        tail = src[idx: idx + 1200]
        assert f'"{forbidden}": True' not in tail, (
            "闲聊分支仍在声明已精排：该分支在检索之前，既没检索也无从精排"
        )
