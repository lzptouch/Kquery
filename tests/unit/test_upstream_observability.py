"""上游观测与降级阶梯（`C-07` 占用率 / `C-10` P95 判据）。

## 三件事

1. **占用率有分母**（`C-07`）：`stats()` 给出 `utilization = in_use / max_permits`，
   指标侧暴露 `kquery_upstream_permits`（分母）与 `kquery_upstream_in_use`（分子）；
2. **多模态通道也进隔离仓**（`C-07`）：此前只有 llm / embedding / rerank / milvus
   四条，多模态那一路「不受限也不可见」；
3. **P95 判据进降级阶梯**（`C-10`）：上游最近变慢（还没失败）就降级——
   熔断是事后判据，P95 是事前判据。
"""
from __future__ import annotations

from app.core.config import Settings
from app.services.upstream import (
    Bulkhead,
    configure_bulkheads,
    bulkhead,
    bulkheads_stats,
)


def _reset_registry():
    import app.services.upstream as up

    with up._registry_lock:
        up._registry = {}


def test_bulkhead_stats_expose_utilization_and_denominator():
    """占用率必须有分母，否则外部算不出「是否打满」。"""
    bh = Bulkhead("llm", permits=4)
    stats = bh.stats()
    assert stats["max_permits"] == 4
    assert stats["utilization"] == 0.0
    with bh.acquire():
        assert bh.stats()["utilization"] == 0.25
    assert bh.stats()["utilization"] == 0.0


def test_bulkhead_records_latency_and_exposes_p95():
    """耗时滚动窗口 → P95（`C-10` 的判据数据源）。"""
    bh = Bulkhead("llm", permits=4)
    assert bh.stats()["p95_ms"] == 0.0
    assert bh.stats()["latency_samples"] == 0

    # 直接注入样本（避免真的 sleep）
    bh._latencies.extend([10.0, 20.0, 30.0, 40.0, 50.0])
    st = bh.stats()
    assert st["latency_samples"] == 5
    assert st["p95_ms"] > 0


def test_multimodal_channel_registered():
    """多模态通道也要有隔离仓（`C-07`）。"""
    _reset_registry()
    settings = Settings(_env_file=None)
    settings.upstream_multimodal_permits = 2
    configure_bulkheads(settings)
    stats = bulkheads_stats()
    assert "multimodal" in stats, f"多模态通道未注册：{sorted(stats)}"
    assert stats["multimodal"]["max_permits"] == 2
    _reset_registry()


def test_degrade_level_two_on_slow_upstream():
    """上游 P95 超阈值 → 降级级别 2（`C-10`）。"""
    from app.services.pipeline import RagPipeline

    settings = Settings(_env_file=None)
    settings.degrade_ladder_enabled = True
    settings.degrade_upstream_slow_ms = 8000

    pipeline = object.__new__(RagPipeline)
    pipeline._settings = settings

    _reset_registry()
    settings.upstream_llm_permits = 2
    configure_bulkheads(settings)

    # 正常速度 → 不降级
    bh = bulkhead("llm")
    bh._latencies.extend([100.0] * 10)
    assert pipeline._degrade_level() == 0

    # 变慢 → 级别 2
    bh._latencies.extend([20000.0] * 10)
    assert pipeline._degrade_level() == 2
    _reset_registry()


def test_degrade_level_two_requires_enough_samples():
    """样本不足时不据此降级（刚启动窗口是空的，P95=0 会被误读成「很快」）。"""
    from app.services.pipeline import RagPipeline

    settings = Settings(_env_file=None)
    settings.degrade_ladder_enabled = True
    settings.degrade_upstream_slow_ms = 10

    pipeline = object.__new__(RagPipeline)
    pipeline._settings = settings

    _reset_registry()
    settings.upstream_llm_permits = 2
    configure_bulkheads(settings)

    bh = bulkhead("llm")
    bh._latencies.extend([99999.0, 99999.0])  # 只有 2 个样本
    assert pipeline._degrade_level() == 0
    _reset_registry()


def test_degrade_ladder_disabled_by_default():
    """降级阶梯默认关闭（阈值未标定前不能开）。"""
    settings = Settings(_env_file=None)
    assert settings.degrade_ladder_enabled is False


def test_slow_threshold_is_recalibrated():
    """阈值必须重新标定：正常单次 LLM 调用 3~8s，2000ms 开着就会一直降级。"""
    settings = Settings(_env_file=None)
    assert settings.degrade_upstream_slow_ms >= 8000, (
        "上游慢阈值偏小：正常单次 LLM 调用就 3~8s，阈值低于 8000ms 会一直降级"
    )
