"""流量优先级与后台让路（`C-11`）。

## 语义

| 优先级 | 端点 | 系统繁忙时的行为 |
|---|---|---|
| 高（交互） | `/ask`、`/ask/stream` | **继续接纳** |
| 低（后台） | `/knowledge/ingest`、`/graph/build`、`/knowledge/rebuild` | **拒绝并返回 503 + `Retry-After`** |

## 为什么

「后台工作把交互服务打挂」是最常见的容量事故：一次批量导入塞满共享排队位，
于是 `/ask` 全部 503——用户以为服务坏了，其实只是有人在导数据。

## 「拒绝」与「等不到」的区别

两者都让 `acquire()` 返回 False，但含义不同：

- **拒绝**（队列满）→ 立即返回，`rejected_total` 递增 → 调用方映射为 503 + Retry-After；
- **等不到**（等待超时）→ 等待后返回 False，`rejected_total` **不**递增。

所以测试要断言的是 `rejected_total`，不是返回值本身。
"""
from __future__ import annotations

import threading
import time


from app.services.pipeline_executor import PipelineExecutor


def _wait_until(predicate, timeout_s: float = 2.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_low_priority_has_smaller_queue_limit():
    """低优先级排队上限更小（后台任务不该占满交互的排队位）。"""
    ex = PipelineExecutor(workers=2, queue_size=100, low_queue_size=25)
    assert ex.stats()["queue_size"] == 100
    assert ex.stats()["queue_size_low"] == 25


def test_low_priority_rejected_when_its_queue_is_full():
    """低优先级排队满 → 立即拒绝（计入拒入计数）。"""
    ex = PipelineExecutor(workers=1, queue_size=100, low_queue_size=1)
    assert ex.acquire(timeout_s=0.1, priority="high") is True  # 占满在飞槽位

    t = threading.Thread(target=lambda: ex.acquire(timeout_s=5.0, priority="low"))
    t.start()
    assert _wait_until(lambda: ex.stats()["waiting"] >= 1)

    rejected_before = ex.stats()["rejected_total"]
    assert ex.acquire(timeout_s=0.1, priority="low") is False
    assert ex.stats()["rejected_total"] == rejected_before + 1, "低优先级队列满应计一次拒入"

    ex.shutdown(timeout_s=1.0)
    t.join(timeout=2)


def test_high_priority_not_rejected_by_low_priority_backlog():
    """低优先级堆满时，高优先级**不会被拒**（它只是等）。"""
    ex = PipelineExecutor(workers=1, queue_size=100, low_queue_size=1)
    assert ex.acquire(timeout_s=0.1) is True
    t = threading.Thread(target=lambda: ex.acquire(timeout_s=5.0, priority="low"))
    t.start()
    assert _wait_until(lambda: ex.stats()["waiting"] >= 1)

    rejected_before = ex.stats()["rejected_total"]
    # 高优先级：等不到槽位（超时返回 False），但**不是被拒**
    assert ex.acquire(timeout_s=0.1, priority="high") is False
    assert ex.stats()["rejected_total"] == rejected_before, "高优先级不该被低优先级积压而拒入"

    ex.shutdown(timeout_s=1.0)
    t.join(timeout=2)


def test_reject_reason_is_reported():
    """拒入要带原因（指标 label `reason`）：低优先级与过载是两种情况。"""
    reasons: list[str] = []
    ex = PipelineExecutor(
        workers=1, queue_size=1, low_queue_size=1,
        on_reject=lambda reason="overloaded": reasons.append(reason),
    )
    assert ex.acquire(timeout_s=0.1) is True
    t = threading.Thread(target=lambda: ex.acquire(timeout_s=3.0, priority="low"))
    t.start()
    assert _wait_until(lambda: ex.stats()["waiting"] >= 1)
    ex.acquire(timeout_s=0.1, priority="low")
    assert "low_priority" in reasons, reasons
    ex.shutdown(timeout_s=1.0)
    t.join(timeout=2)


def test_background_gate_returns_503_with_retry_after(client, monkeypatch):
    """后台端点在系统繁忙时返回 503 + Retry-After（而不是笼统的 500）。"""
    executor = client.app.state.pipeline_executor
    original = executor.stats

    def _busy_stats():
        s = original()
        return {**s, "waiting": s["queue_size"]}  # 伪装成排队已满

    monkeypatch.setattr(executor, "stats", _busy_stats)

    # `/graph/build` 已随 `C-09` 移出后台闸门（改走独立的单 worker 队列），
    # 这里必须用仍挂在闸门上的后台端点：`/rebuild`。依赖先于 handler 执行，
    # 所以 doc 是否存在不影响这条断言（busy → 直接 503）。
    resp = client.post("/knowledge/rebuild/any-doc")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == int(6101)  # RATE_LIMITED
    assert resp.headers.get("Retry-After"), "503 必须带 Retry-After"


def test_graph_build_not_gated_by_background_rule(client, monkeypatch):
    """`C-09`：图谱构建有自己的有界队列，不再占用后台闸门名额。

    同步版本在构建期间会一直持有 `reject_background_when_busy` 的槽位，
    导致一次全量重建期间所有后台操作都被拒——这正是要修掉的耦合。
    """
    executor = client.app.state.pipeline_executor
    original = executor.stats

    def _busy_stats():
        s = original()
        return {**s, "waiting": s["queue_size"]}

    monkeypatch.setattr(executor, "stats", _busy_stats)

    resp = client.post("/graph/build", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["task_id"].startswith("gb-")


def test_interactive_endpoint_not_gated_by_background_rule(client, monkeypatch):
    """交互端点不受后台闸门影响（`/ask` 继续接纳）。"""
    executor = client.app.state.pipeline_executor
    original = executor.stats

    def _busy_stats():
        s = original()
        return {**s, "waiting": s["queue_size"]}

    monkeypatch.setattr(executor, "stats", _busy_stats)

    resp = client.post("/ask", json={"question": "系统繁忙时也要能问"})
    assert resp.status_code == 200, resp.text
