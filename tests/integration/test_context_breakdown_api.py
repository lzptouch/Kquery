"""`/ask` 响应里的上下文构成表（`ST-01` 阶段 A）。

## 三条验收

1. `/ask` 响应含**顶层字段** `context_breakdown`；
2. **五个来源键齐全**（没有内容的来源给 0，而不是缺键）；
3. **死字段回归**：被丢弃的条目出现在 `context_breakdown.dropped` 里
   （此前 `pipeline.py` 的被丢弃列表**没有任何消费点**——「给前端解释」只是注释里的意图）。
"""
from __future__ import annotations

from tests.conftest import SAMPLE_MD


def _upload(client) -> None:
    resp = client.post(
        "/knowledge/upload",
        files={"file": ("产品手册.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200, resp.text


def test_ask_response_contains_context_breakdown(client):
    """① 响应含顶层 `context_breakdown`。"""
    _upload(client)
    resp = client.post("/ask", json={"question": "支持哪些文档格式？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert "context_breakdown" in data
    breakdown = data["context_breakdown"]
    assert isinstance(breakdown, dict)
    assert "sources" in breakdown
    assert "total_tokens" in breakdown


def test_five_source_keys_are_present(client):
    """② 五个来源键齐全（前端不用处理「键时有时无」）。"""
    _upload(client)
    resp = client.post("/ask", json={"question": "计费规则是什么？"})
    data = resp.json()["data"]
    sources = data["context_breakdown"]["sources"]
    assert set(sources) == {"system", "history", "evidence", "tools", "memory"}
    for name, info in sources.items():
        assert "count" in info and "tokens" in info and "quota" in info, name


def test_dropped_items_are_exposed(client, monkeypatch):
    """③ 死字段回归：被丢弃的条目出现在 `context_breakdown.dropped`。"""
    _upload(client)
    # 把上下文预算压到极小 → 证据会被裁 → 必须能看到丢弃原因
    settings = client.app.state.settings
    monkeypatch.setattr(settings, "context_token_budget", 20, raising=False)

    resp = client.post("/ask", json={"question": "支持哪些文档格式？"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    breakdown = data["context_breakdown"]
    assert "dropped" in breakdown
    assert "dropped_count" in breakdown
    # 预算极小时至少应有条目被裁（否则「死字段」又回来了）
    assert breakdown["dropped_count"] > 0, breakdown
    assert all(d.get("reason") for d in breakdown["dropped"]), breakdown["dropped"]


def test_breakdown_total_does_not_exceed_budget(client):
    """构成表的 token 总量不应超过可用预算。"""
    _upload(client)
    resp = client.post("/ask", json={"question": "产品简介"})
    data = resp.json()["data"]
    breakdown = data["context_breakdown"]
    assert (
        breakdown["total_tokens"] <= breakdown["available_tokens"]
        or breakdown["available_tokens"] == 0
    )
