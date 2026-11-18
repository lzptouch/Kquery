"""灰度自动回滚（`OP-02`）。

## 六条验收

1. 劣化超阈值 → 判定回滚且覆盖层被 `clear_scoped` 移除
2. 无基线 → **只告警、不回滚**
3. 未劣化 → 不动
4. **默认关闭时判定无任何副作用**
5. 样本数低于下限 → 不判劣化
6. **多副本源未配置 `prometheus_url` → 返回 None，不猜数**
"""
from __future__ import annotations


from app.services.canary import (
    ACTION_ALERT,
    ACTION_NO_DATA,
    ACTION_OK,
    ACTION_ROLLBACK,
    CanaryController,
    CanaryRule,
    InProcessMetricSource,
    MultiReplicaMetricSource,
    evaluate_rule,
)


class _Store:
    """假配置存储：记录 `clear_scoped` 调用。"""

    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear_scoped(self, scope: str) -> bool:
        self.cleared.append(scope)
        return True


class _Source:
    def __init__(self, value: float | None, samples: int = 100) -> None:
        self._value = value
        self._samples = samples

    def value(self, metric: str, scope: str):
        return self._value, self._samples


def _rule(**overrides) -> CanaryRule:
    base = dict(metric="error_rate", direction="gt", threshold=0.05, min_samples=10)
    base.update(overrides)
    return CanaryRule(**base)


def test_degradation_triggers_rollback_and_clears_scope():
    """① 劣化超阈值 → 判定回滚且覆盖层被移除。"""
    store = _Store()
    controller = CanaryController(
        config_store=store,
        metric_source=_Source(0.2),
        baseline_provider=lambda metric, scope: 0.01,
        rules=[_rule()],
        enabled=True,
    )
    verdict = controller.rollback("tenant:t1")
    assert verdict.action == ACTION_ROLLBACK
    assert verdict.should_rollback is True
    assert store.cleared == ["tenant:t1"], "回滚必须移除覆盖层"


def test_no_baseline_only_alerts():
    """② 无基线 → 只告警、不回滚。"""
    store = _Store()
    controller = CanaryController(
        config_store=store,
        metric_source=_Source(0.9),
        baseline_provider=lambda metric, scope: None,
        rules=[_rule()],
        enabled=True,
    )
    verdict = controller.rollback("tenant:t1")
    assert verdict.action == ACTION_NO_DATA
    assert not store.cleared, "无基线时不能回滚（不能拿「无数据」当「劣化」）"


def test_no_degradation_leaves_scope_untouched():
    """③ 未劣化 → 不动。"""
    store = _Store()
    controller = CanaryController(
        config_store=store,
        metric_source=_Source(0.001),
        baseline_provider=lambda metric, scope: 0.01,
        rules=[_rule()],
        enabled=True,
    )
    verdict = controller.rollback("tenant:t1")
    assert verdict.action == ACTION_OK
    assert not store.cleared


def test_disabled_by_default_has_no_side_effect():
    """④ 默认关闭时判定无任何副作用。"""
    store = _Store()
    controller = CanaryController(
        config_store=store,
        metric_source=_Source(0.99),
        baseline_provider=lambda metric, scope: 0.0,
        rules=[_rule()],
        enabled=False,
    )
    verdict = controller.rollback("tenant:t1")
    assert verdict.action == ACTION_OK
    assert "未启用" in verdict.reason
    assert not store.cleared


def test_small_sample_does_not_judge_degradation():
    """⑤ 样本数低于下限 → 不判劣化。"""
    store = _Store()
    controller = CanaryController(
        config_store=store,
        metric_source=_Source(0.9, samples=3),
        baseline_provider=lambda metric, scope: 0.0,
        rules=[_rule(min_samples=30)],
        enabled=True,
    )
    verdict = controller.rollback("tenant:t1")
    assert verdict.action == ACTION_ALERT
    assert not store.cleared


def test_multi_replica_source_without_url_returns_none():
    """⑥ 多副本源未配置 `prometheus_url` → 返回 None，不猜数。"""
    source = MultiReplicaMetricSource("")
    assert source.configured is False
    value, samples = source.value("error_rate", "tenant:t1")
    assert value is None, "未配置 Prometheus 时必须返回 None，不能猜"
    assert samples == 0


def test_relative_rule_uses_baseline_ratio():
    """相对基线的劣化判定（阈值解释为比例）。"""
    rule = _rule(relative=True, threshold=0.5)
    verdict = evaluate_rule(rule, 0.2, 0.1, samples=100)
    assert verdict.action == ACTION_ROLLBACK, verdict.reason
    verdict2 = evaluate_rule(rule, 0.11, 0.1, samples=100)
    assert verdict2.action == ACTION_OK


def test_absolute_threshold_crossing_triggers_rollback():
    """绝对阈值：实验组越过阈值即劣化（延迟 > 500ms）。"""
    rule = _rule(metric="p95_latency_ms", direction="gt", threshold=500)
    verdict = evaluate_rule(rule, 900, 300, samples=100)
    assert verdict.action == ACTION_ROLLBACK, verdict.reason
    # 没越过 → 不劣化
    assert evaluate_rule(rule, 300, 300, samples=100).action == ACTION_OK


def test_lt_direction_detects_hit_rate_drop():
    """`lt` 方向：低于阈值算劣化（如命中率跌破 0.95）。"""
    rule = _rule(metric="hit_rate", direction="lt", threshold=0.95)
    verdict = evaluate_rule(rule, 0.80, 0.98, samples=100)
    assert verdict.action == ACTION_ROLLBACK, verdict.reason
    assert evaluate_rule(rule, 0.99, 0.98, samples=100).action == ACTION_OK


def test_in_process_source_reads_error_rate():
    """进程内指标源能读出 error_rate 与样本数。"""
    class _Store2:
        def summary(self, range_s: int = 3600):
            return {"total_requests": 120, "error_rate": 0.03}

    source = InProcessMetricSource(_Store2())
    value, samples = source.value("summary", "tenant:t1")
    assert value == 0.03
    assert samples == 120


def test_in_process_source_returns_none_for_missing_metric():
    """指标不存在 → None（不是 0）。"""
    source = InProcessMetricSource(object())
    value, samples = source.value("summary", "tenant:t1")
    assert value is None
    assert samples == 0
