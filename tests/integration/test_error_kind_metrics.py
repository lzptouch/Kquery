"""集成：错误指标能被真实请求序列触发（`A-09` 验收）。

验收标准（设计文档 §五 1）：**集成测试能触发 ≥3 个不同 `kind`**。

单元测试证明「映射函数算得对」，但不能证明「指标真的被写进去了」——
所以这里用真实 HTTP 请求触发 4 类错误，再从 `/metrics` 里把 `kind` 抠出来数。
"""
from __future__ import annotations

import re

_KIND_RE = re.compile(r'kquery_errors_total\{code="\d+",kind="([a-z_]+)"\}')

# 只放行低基数标签：出现 trace_id / 路径 / 用户就是高基数反模式
_HIGH_CARDINALITY_TOKENS = ("trace_id", "path=", "user", "tenant", "question")


def _kinds(metrics_text: str) -> set[str]:
    return set(_KIND_RE.findall(metrics_text))


def test_error_metric_triggers_multiple_kinds(client, monkeypatch):
    """一次请求序列触发 ≥3 类错误，并在 /metrics 里可见。"""
    # ① 输入类：空问题 → 400（QUESTION_EMPTY 2002）
    resp = client.post("/ask", json={"question": ""})
    assert resp.status_code == 400, resp.text

    # ② 资源类：素材不存在 → 404（ASSET_NOT_FOUND 3302）
    resp = client.get("/assets/does-not-exist.png")
    assert resp.status_code == 404, resp.text

    # ③ 限流类：把令牌桶压到「几乎不补充」，第二个请求必然 429
    store = client.app.state.config_store
    original_get_active = store.get_active
    active = original_get_active()
    monkeypatch.setattr(
        store,
        "get_active",
        lambda: active.model_copy(update={"ratelimit_capacity": 1, "ratelimit_per_min": 0.001}),
    )
    first = client.post("/ask", json={"question": "限流触发一次"})
    assert first.status_code == 200, first.text
    second = client.post("/ask", json={"question": "限流触发二次"})
    assert second.status_code == 429, second.text
    assert second.json()["error"]["code"] == int(6101)
    # 恢复限流配置：后续用例还要正常发请求
    monkeypatch.setattr(store, "get_active", original_get_active)

    # ④ 内部类：让问答链路抛未捕获异常 → 500（INTERNAL_ERROR 5001）
    from app.services.pipeline import RagPipeline

    def _boom(*args, **kwargs):
        raise RuntimeError("测试用：内部错误")

    monkeypatch.setattr(RagPipeline, "ask", _boom)
    resp = client.post("/ask", json={"question": "触发内部错误"})
    assert resp.status_code == 500, resp.text

    metrics_text = client.get("/metrics").text
    kinds = _kinds(metrics_text)
    assert len(kinds) >= 3, f"只触发了 {len(kinds)} 类 kind：{kinds}\n{metrics_text}"
    assert {"input", "resource", "ratelimit"} <= kinds, kinds


def test_error_metric_has_no_high_cardinality_labels(client):
    """标签纪律：只有 code 与 kind。"""
    client.post("/ask", json={"question": ""})
    metrics_text = client.get("/metrics").text
    for line in metrics_text.splitlines():
        if not line.startswith("kquery_errors_total"):
            continue
        for token in _HIGH_CARDINALITY_TOKENS:
            assert token not in line, f"错误指标出现高基数标签 {token}：{line}"
