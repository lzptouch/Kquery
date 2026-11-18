"""图存储锁等待指标（`C-08`）。

## 为什么要有

图谱存储是**单连接 + 全局锁**（SQLite 单写者语义）。Agent 并发 >16 时，
它**可能**成为瓶颈——但此前**没有任何指标能证明**：
「可能」与「已证实」之间隔着一次压测，而没有指标就做不了这次压测。

## 判据（设计文档 §十）

> 锁等待 P95 > 锁持有 P95 的 1/3 → 判定图写入成为瓶颈 → 触发 `C-09`。

所以指标必须**同时**给出「等了多久」与「持有了多久」——只给一个没法比。
"""
from __future__ import annotations

import threading
import time

from app.services.graph.sqlite_store import SqliteGraphStore, _TimedLock


def test_timed_lock_reports_wait_and_hold():
    """一次加解锁 → 回调拿到 (wait_s, hold_s) 两个量。"""
    seen: list[tuple[str, float, float]] = []

    lock = _TimedLock("sqlite", lambda st, w, h: seen.append((st, w, h)))
    lock.acquire()
    time.sleep(0.01)  # 持有 10ms
    lock.release()

    assert len(seen) == 1
    store_type, wait_s, hold_s = seen[0]
    assert store_type == "sqlite"
    assert wait_s >= 0.0
    assert hold_s >= 0.005, f"持有耗时应不少于 5ms，实际 {hold_s}"


def test_timed_lock_measures_wait_time_under_contention():
    """并发争抢时，等待时长应大于 0（这就是「等锁」的度量）。"""
    seen: list[tuple[str, float, float]] = []
    lock = _TimedLock("sqlite", lambda st, w, h: seen.append((st, w, h)))

    lock.acquire()  # 占住
    released = threading.Event()

    def _waiter() -> None:
        lock.acquire()
        released.set()
        lock.release()

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.05)  # 让它真的在等
    lock.release()  # 放行
    t.join(timeout=2)
    assert released.is_set()

    # 两次：第一次（主线程）等待≈0，第二次（等待者）等待≥0.04
    assert len(seen) == 2
    waits = sorted(w for _, w, _ in seen)
    assert waits[-1] >= 0.03, f"等待耗时应被记录，实际 {waits}"


def test_timing_callback_failure_does_not_break_locking():
    """指标回调失败不该让图写入失败（否则观测能力变成故障源）。"""
    def _boom(st, w, h):
        raise RuntimeError("metrics down")

    lock = _TimedLock("sqlite", _boom)
    lock.acquire()
    lock.release()  # 不抛异常即通过


def test_sqlite_store_passes_lock_timing_to_callback(tmp_path):
    """SQLite 图存储的锁计时会回调到装配层。"""
    seen: list[tuple[str, float, float]] = []
    store = SqliteGraphStore(tmp_path / "graph.db", on_lock_timing=lambda st, w, h: seen.append((st, w, h)))
    store.upsert_entity(_entity("苹果公司", "default"))
    assert seen, "图存储写操作应产生锁计时样本"
    assert all(s[0] == "sqlite" for s in seen)


def test_metrics_expose_lock_histograms():
    """`/metrics` 侧要有这两个直方图（按存储类型打标签）。"""
    from app.core.metrics import PromMetrics

    prom = PromMetrics()
    prom.graph_lock_wait_seconds.labels(store="sqlite").observe(0.01)
    prom.graph_lock_hold_seconds.labels(store="sqlite").observe(0.02)
    text = prom.render().decode("utf-8")
    assert "kquery_graph_lock_wait_seconds" in text
    assert "kquery_graph_lock_hold_seconds" in text
    assert 'store="sqlite"' in text


def _entity(name: str, tenant: str):
    from app.domain.schemas import EntityNode

    return EntityNode(entity_id="e1", name=name, tenant_id=tenant, type="org", source_doc="d1")
