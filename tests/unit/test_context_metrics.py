"""上下文构成的聚合与端点（`ST-01` 阶段 A）。

## 三件事

1. `MetricsStore.record_context()` 累加构成（五个来源 + 丢弃原因）；
2. **有界**：丢弃原因用固定容量 LRU（不因请求数增长而无限膨胀）；
3. `/admin/metrics/context` 端点能读到聚合结果。
"""
from __future__ import annotations


def _breakdown(evidence_tokens: int = 100, dropped_reason: str | None = None) -> dict:
    data = {
        "total_tokens": evidence_tokens,
        "budget_tokens": 6000,
        "available_tokens": 4500,
        "sources": {
            "system": {"count": 1, "tokens": 10, "quota": 225},
            "history": {"count": 0, "tokens": 0, "quota": 450},
            "evidence": {"count": 2, "tokens": evidence_tokens, "quota": 2700},
            "tools": {"count": 1, "tokens": 20, "quota": 900},
            "memory": {"count": 0, "tokens": 0, "quota": 225},
        },
        "dropped": [],
        "dropped_count": 0,
    }
    if dropped_reason:
        data["dropped"] = [
            {"source": "evidence", "key": "c9", "tokens": 50, "reason": dropped_reason}
        ]
        data["dropped_count"] = 1
    return data


def test_record_context_accumulates_sources():
    """① 累加五个来源的计数与 token。"""
    from app.core.metrics import MetricsStore

    store = MetricsStore()
    store.record_context(_breakdown(100))
    store.record_context(_breakdown(200))
    summary = store.context_summary()
    assert summary["requests"] == 2
    assert summary["sources"]["evidence"]["count"] == 4
    assert summary["sources"]["evidence"]["tokens"] == 300
    assert summary["avg_total_tokens"] == 150.0


def test_dropped_reasons_are_counted():
    """② 丢弃原因计数（用于标定配额）。"""
    from app.core.metrics import MetricsStore

    store = MetricsStore()
    store.record_context(_breakdown(100, "超出 evidence 配额（2700 tokens）"))
    store.record_context(_breakdown(100, "超出 evidence 配额（2700 tokens）"))
    store.record_context(_breakdown(100, "超出 history 配额（450 tokens）"))
    summary = store.context_summary()
    top = summary["dropped_top"]
    assert top[0]["reason"].startswith("超出 evidence")
    assert top[0]["count"] == 2
    assert len(top) == 2


def test_dropped_reasons_are_bounded():
    """③ 有界：丢弃原因的数量**不随请求数无限增长**（`A-21` 的教训）。"""
    from app.core.metrics import MetricsStore

    store = MetricsStore()
    for i in range(200):
        store.record_context(_breakdown(10, f"原因-{i}"))
    summary = store.context_summary()
    assert len(summary["dropped_top"]) <= 10  # 只返回 Top 10
    assert len(store._context["dropped"]) <= store._context_dropped_capacity


def test_empty_breakdown_is_ignored():
    """空构成（缓存命中等路径）不计数。"""
    from app.core.metrics import MetricsStore

    store = MetricsStore()
    store.record_context({})
    assert store.context_summary()["requests"] == 0


def test_admin_endpoint_returns_context_summary(client):
    """④ `/admin/metrics/context` 能读到聚合结果。"""
    resp = client.get("/admin/metrics/context")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    data = body["data"]
    for key in ("requests", "avg_total_tokens", "sources", "dropped_top"):
        assert key in data, key


def test_endpoint_reflects_real_requests(client):
    """端点数据来自真实请求（上传 + 问答各一次）。"""
    from tests.conftest import SAMPLE_MD

    client.post(
        "/knowledge/upload",
        files={"file": ("产品手册.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    client.post("/ask", json={"question": "支持哪些文档格式？"})
    data = client.get("/admin/metrics/context").json()["data"]
    assert data["requests"] >= 1
    assert data["sources"].get("evidence", {}).get("count", 0) >= 1
