"""工具失败率指标（`MP-01`）。

## 为什么要有

「工具失败率」是工具可用性的**先行指标**：工具开始大面积失败时，
答案质量**还没掉**（Agent 可能重试成功），但已经不该继续用这条链路了。

此前 Agent 指标只有「调用分布」，没有失败计数——**分布看不出失败**。

## 四条验收

1. 失败计数正确（只算带 `error` 的调用）
2. 失败率 = 失败数 / 调用总数（没有调用时为 0，不是 None）
3. 按工具的失败分布
4. 失败会进 `abnormal` 列表（可追溯到具体工具与原因）
"""
from __future__ import annotations

from app.core.metrics import MetricsStore


def _record(store: MetricsStore, calls: list[dict]) -> None:
    store.record_agent_run(
        iterations=1, fused=False, fuse_reason="", tool_calls=calls, session_id="s1"
    )


def test_failures_are_counted():
    """① 只算带 `error` 的调用。"""
    store = MetricsStore()
    _record(store, [
        {"tool_name": "search", "error": ""},
        {"tool_name": "calc", "error": "boom"},
        {"tool_name": "search", "error": "超时"},
    ])
    summary = store.agent_summary()
    assert summary["tool_calls_total"] == 3
    assert summary["tool_failures"] == 2
    assert summary["tool_failure_rate"] == round(2 / 3, 4)


def test_no_tool_calls_gives_zero_rate():
    """② 没有调用时失败率为 0（不是 None，也不是除零）。"""
    store = MetricsStore()
    store.record_agent_run(iterations=1, fused=False, fuse_reason="", tool_calls=[])
    summary = store.agent_summary()
    assert summary["tool_calls_total"] == 0
    assert summary["tool_failure_rate"] == 0.0


def test_failure_distribution_by_tool():
    """③ 按工具的失败分布。"""
    store = MetricsStore()
    _record(store, [
        {"tool_name": "search", "error": "a"},
        {"tool_name": "search", "error": "b"},
        {"tool_name": "calc", "error": "c"},
    ])
    summary = store.agent_summary()
    assert summary["tool_failure_distribution"] == {"search": 2, "calc": 1}


def test_failures_appear_in_abnormal_list():
    """④ 失败进 `abnormal`（可追溯到工具与原因）。"""
    store = MetricsStore()
    _record(store, [{"tool_name": "search", "error": "超时了"}])
    summary = store.agent_summary()
    kinds = [a["kind"] for a in summary["abnormal"]]
    assert "tool_error" in kinds
    entry = next(a for a in summary["abnormal"] if a["kind"] == "tool_error")
    assert "search" in entry["detail"]
