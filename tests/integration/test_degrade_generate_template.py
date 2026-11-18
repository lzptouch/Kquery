"""生成降级模板（`C-12`）。

## 语义

上游不可用（熔断 / 超时 / 配额）时，`/ask` **不返回 5xx**，而是：

- HTTP 200；
- `verdict = "refused"`；
- `refusal_reason = "upstream_unavailable"`；
- 答案是一段**固定的降级文案**（预设，不是模板变量填充）。

## 为什么不是 5xx

5xx 的语义是「服务坏了，重试可能有用」；而上游熔断时用户真正需要知道的是
「现在答不了，等一会儿再来」——把它包装成 502 会让网关与前端都当成故障处理。
"""
from __future__ import annotations

from app.core.envelope import ErrCode
from app.domain.constants import REFUSAL_REASON_UPSTREAM_UNAVAILABLE, UPSTREAM_UNAVAILABLE_TEXT
from app.services.llm import LLMError
from app.services.pipeline import RagPipeline
from app.services.upstream import CircuitOpenError


def _break_llm(monkeypatch):
    def _boom(*args, **kwargs):
        raise LLMError(ErrCode.GENERATION_FAILED, "上游模型不可用（测试注入）")

    monkeypatch.setattr(RagPipeline, "_llm_generate", _boom)


def test_upstream_failure_returns_200_with_degradation_text(client, monkeypatch):
    """上游故障 → 200 + 拒答语义 + 可读文案。"""
    _break_llm(monkeypatch)
    resp = client.post("/ask", json={"question": "上游挂了会怎样"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] in ("ok", "warn")
    data = body["data"]
    assert data["verdict"] == "refused"
    assert data["refusal_reason"] == REFUSAL_REASON_UPSTREAM_UNAVAILABLE
    assert data["answer"] == UPSTREAM_UNAVAILABLE_TEXT
    assert data["evidence"] == []


def test_circuit_open_also_degrades(client, monkeypatch):
    """熔断打开同样降级（而不是 5xx）。"""
    def _boom(*args, **kwargs):
        raise CircuitOpenError("llm", 30.0)

    monkeypatch.setattr(RagPipeline, "_llm_generate", _boom)
    resp = client.post("/ask", json={"question": "熔断了会怎样"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["refusal_reason"] == REFUSAL_REASON_UPSTREAM_UNAVAILABLE


def test_non_upstream_error_still_fails(client, monkeypatch):
    """**非**上游的异常照旧 5xx——把所有异常都变成「稍后重试」等于把 bug 藏起来。"""
    def _boom(*args, **kwargs):
        raise ValueError("程序 bug（测试注入）")

    monkeypatch.setattr(RagPipeline, "_llm_generate", _boom)
    resp = client.post("/ask", json={"question": "普通异常会怎样"})
    assert resp.status_code >= 500, resp.text


def test_degradation_is_counted(client, monkeypatch):
    """降级要留下痕迹（指标），否则「答不出来」与「答得慢」无法区分。"""
    _break_llm(monkeypatch)
    client.post("/ask", json={"question": "降级要计数"})
    metrics_text = client.get("/metrics").text
    assert "kquery_shed_total{reason=\"degrade_generate\"}" in metrics_text, (
        "降级没有计入 shed_total"
    )
