"""DeepSeek provider 与「三个 provider 解耦」的解析口径（`LM-01` 相关）。

## 关键事实：DeepSeek 不提供 embeddings / rerank

DeepSeek 官方 API **只有 `/chat/completions`**。所以：
- 生成模型可以走 DeepSeek；
- 向量化与重排**不能**用 DeepSeek 的 key 直连（会 404/400）。

配置层对此的处理（`core/config.py` 的 `resolved_*_provider`）：
`auto` 遇到 `deepseek` 时，向量与重排**回落 dashscope**，
而不是跟着去调一个必然失败的端点。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.envelope import ErrCode
from app.services.llm import LLMError, build_llm


def test_build_llm_dispatch_deepseek():
    """provider=deepseek → DeepSeekLLM（不是 Mock）。"""
    from app.services.llm import DeepSeekLLM

    llm = build_llm(
        provider="deepseek",
        api_key="k",
        model="deepseek-chat",
        temperature=0.0,
        max_retries=1,
        timeout_s=5.0,
        base_url="https://api.deepseek.com",
    )
    assert isinstance(llm, DeepSeekLLM)
    assert llm.model == "deepseek-chat"


def test_deepseek_endpoint_and_payload(monkeypatch):
    """请求打到 `{base}/chat/completions`，请求体是 OpenAI 兼容格式。"""
    from app.services import openai_compat

    seen: dict = {}

    def _fake_stream_lines(url, payload, api_key, timeout_s=30.0):
        seen["url"] = url
        seen["payload"] = payload
        seen["api_key"] = api_key
        yield '{"choices":[{"delta":{"content":"你好"}}]}'
        yield '{"choices":[{"delta":{"content":"，世界"}}]}'
        yield "[DONE]"

    monkeypatch.setattr(openai_compat, "stream_lines", _fake_stream_lines)

    llm = build_llm(
        provider="deepseek", api_key="secret-key", model="deepseek-chat",
        temperature=0.0, max_retries=1, timeout_s=5.0,
        base_url="https://api.deepseek.com",
    )
    text = llm.generate("问题")
    assert text == "你好，世界"
    assert seen["url"] == "https://api.deepseek.com/chat/completions"
    assert seen["payload"]["model"] == "deepseek-chat"
    assert seen["payload"]["messages"] == [{"role": "user", "content": "问题"}]
    assert seen["api_key"] == "secret-key"
    # 凭据不能出现在异常/日志里
    assert "secret-key" not in text


def test_deepseek_auth_error_fails_fast(monkeypatch):
    """401 → 立刻失败，不重试（重试没有意义）。"""
    from app.services import openai_compat

    calls = {"n": 0}

    def _fake_stream_lines(url, payload, api_key, timeout_s=30.0):
        calls["n"] += 1
        raise openai_compat.OpenAICompatError(401, "invalid api key")
        yield  # pragma: no cover

    monkeypatch.setattr(openai_compat, "stream_lines", _fake_stream_lines)

    llm = build_llm(
        provider="deepseek", api_key="bad", model="deepseek-chat",
        temperature=0.0, max_retries=3, timeout_s=5.0,
    )
    with pytest.raises(LLMError) as excinfo:
        llm.generate("问题")
    assert calls["n"] == 1, "鉴权失败不应重试"
    assert int(excinfo.value.code) == int(ErrCode.GENERATION_FAILED)


def test_deepseek_rate_limit_maps_to_rate_limited(monkeypatch):
    """429 → 限流码（调用方知道该退避而不是改请求）。"""
    from app.services import openai_compat

    def _fake_stream_lines(url, payload, api_key, timeout_s=30.0):
        raise openai_compat.OpenAICompatError(429, "rate limited")
        yield  # pragma: no cover

    monkeypatch.setattr(openai_compat, "stream_lines", _fake_stream_lines)
    llm = build_llm(
        provider="deepseek", api_key="k", model="deepseek-chat",
        temperature=0.0, max_retries=1, timeout_s=5.0,
    )
    with pytest.raises(LLMError) as excinfo:
        llm.generate("问题")
    assert int(excinfo.value.code) == int(ErrCode.RATE_LIMITED)


# ---------- 配置解析：三个 provider 解耦 ----------


def test_auto_provider_prefers_deepseek_when_key_present():
    """auto：配了 DeepSeek key 就走 DeepSeek（用户显式配的凭据优先）。"""
    s = Settings(_env_file=None)
    s.model_provider = "auto"
    s.deepseek_api_key = "sk-test"
    s.dashscope_api_key = ""
    assert s.resolved_provider == "deepseek"
    assert s.resolved_llm_api_key == "sk-test"


def test_embedding_and_rerank_fall_back_to_dashscope_with_deepseek():
    """**DeepSeek 不提供 embeddings / rerank** → auto 时回落 dashscope。"""
    s = Settings(_env_file=None)
    s.model_provider = "deepseek"
    s.deepseek_api_key = "sk-test"
    assert s.resolved_embedding_provider == "dashscope"
    assert s.resolved_rerank_provider == "dashscope"


def test_explicit_openai_compatible_provider_is_respected():
    """显式指定 openai_compatible → 不回落（这是「提供这两个端点的服务」）。"""
    s = Settings(_env_file=None)
    s.model_provider = "deepseek"
    s.deepseek_api_key = "sk-test"
    s.embedding_provider = "openai_compatible"
    s.embedding_base_url = "https://gw.example.com/v1"
    s.rerank_provider = "openai_compatible"
    s.rerank_base_url = "https://gw.example.com/v1"
    assert s.resolved_embedding_provider == "openai_compatible"
    assert s.resolved_rerank_provider == "openai_compatible"


def test_invalid_provider_rejected_at_startup():
    """非法 provider 必须在启动校验里被挡住（不能等到第一次调用）。"""
    s = Settings(_env_file=None)
    s.embedding_provider = "deepseek"  # DeepSeek 没有 embeddings 端点
    issues = s.validate_startup()
    assert any(i["field"] == "embedding_provider" for i in issues), issues


def test_base_url_must_be_absolute():
    """base_url 必须是绝对地址（相对地址在容器里必然连错）。"""
    s = Settings(_env_file=None)
    s.embedding_provider = "openai_compatible"
    s.embedding_base_url = "gw.example.com/v1"
    issues = s.validate_startup()
    assert any(i["field"] == "embedding_base_url" for i in issues), issues
