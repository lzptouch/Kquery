"""领域异常在请求链路上的可观察行为（`A-07` 验收）。

验收标准（设计文档 §四 2）：
- 领域异常 → **正确的 HTTP 状态**（不是笼统的 500）；
- 响应体是统一信封，`error.code` 是领域码；
- 错误指标按领域码计数；
- `/ask` 的 LLM 故障返回 **502** 而不是 200。
"""
from __future__ import annotations

import re

from app.core.envelope import ErrCode
from app.core.errors import ConfigError, UpstreamUnavailableError
from app.services.llm import LLMError
from app.services.pipeline import RagPipeline

_KIND_RE = re.compile(r'kquery_errors_total\{code="(\d+)",kind="([a-z_]+)"\}')


def _error_codes(metrics_text: str) -> set[str]:
    return {m.group(1) for m in _KIND_RE.finditer(metrics_text)}


def test_domain_error_maps_to_correct_status_and_code(client, monkeypatch):
    """领域异常 → 正确的 HTTP 状态 + 统一信封 + 按码计数。"""
    def _boom(*args, **kwargs):
        raise ConfigError("CRAG 阈值配置非法（测试注入）")

    monkeypatch.setattr(RagPipeline, "ask", _boom)
    resp = client.post("/ask", json={"question": "触发领域异常"})

    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == int(ErrCode.CONFIG_INVALID)
    # 堆栈不进响应体
    assert "Traceback" not in resp.text
    assert "boom" not in resp.text

    metrics_text = client.get("/metrics").text
    assert str(int(ErrCode.CONFIG_INVALID)) in _error_codes(metrics_text)


def test_upstream_error_maps_to_502(client, monkeypatch):
    """上游故障 → 502（可重试），不是 500。"""
    def _boom(*args, **kwargs):
        raise UpstreamUnavailableError("向量库不可达（测试注入）")

    monkeypatch.setattr(RagPipeline, "ask", _boom)
    resp = client.post("/ask", json={"question": "触发上游故障"})
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == int(ErrCode.DEPENDENCY_UNAVAILABLE)


def test_llm_failure_returns_502_not_200(client, monkeypatch):
    """`/ask` 的 LLM 故障必须是 502 —— 网关按 5xx 统计时不能漏。"""
    def _boom(*args, **kwargs):
        raise LLMError(ErrCode.GENERATION_FAILED, "模型调用失败（测试注入）")

    monkeypatch.setattr(RagPipeline, "ask", _boom)
    resp = client.post("/ask", json={"question": "触发模型故障"})
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["code"] == int(ErrCode.GENERATION_FAILED)


def test_domain_error_is_counted_once(client, monkeypatch):
    """一次异常只计一次（异常处理器计数，抛错点不重复计数）。"""
    def _boom(*args, **kwargs):
        raise ConfigError("只应计一次（测试注入）")

    monkeypatch.setattr(RagPipeline, "ask", _boom)
    client.post("/ask", json={"question": "计数一次"})
    metrics_text = client.get("/metrics").text
    matching = [
        line for line in metrics_text.splitlines()
        if line.startswith(f'kquery_errors_total{{code="{int(ErrCode.CONFIG_INVALID)}"')
    ]
    assert len(matching) == 1, matching
    assert matching[0].endswith(" 1.0"), matching[0]
