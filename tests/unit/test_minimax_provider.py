"""MiniMax provider：解析口径、模型名替换、业务错误翻译。

## 与 DeepSeek 的两点实质差异

1. **错误表意位置**：MiniMax 用 **HTTP 200 + `base_resp.status_code`**，
   不检查就会把错误当成功（空 delta = 「模型没话说」）。
2. **模型名**：`llm_model` / `intent_model` / `agent_planner_model` 的出厂默认是
   千问模型名。只改 `MODEL_PROVIDER` 不改模型名，首次请求就会 404——
   解析期按 `PROVIDER_MODEL_DEFAULTS` 替换，并在 `provider_notes` 里说出来。

**与 DeepSeek 的第三点差异（实测修正）**：MiniMax **有** embeddings
（`embo-01`，`POST /v1/embeddings`，1536 维，请求体是 `texts`/`type` 而非 OpenAI 的 `input`），
所以向量通道在 `auto` 下**跟着 MiniMax 走**；rerank / OCR 仍没有，回落 dashscope。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.core.envelope import ErrCode
from app.services.llm import LLMError, MiniMaxLLM, MockLLM, build_llm


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """隔离宿主机环境变量（单文件跑绿、全量跑红的根因就在这里）。

    `Settings(_env_file=None)` 只关掉 `.env` 文件，**关不掉 `os.environ`**：
    `pymilvus` 在 import 时执行 `load_dotenv()`，会把 `backend/.env` 整份注入
    `os.environ`（本机就写着 `MODEL_PROVIDER=deepseek` / `LLM_MODEL=deepseek-chat`）。
    pydantic-settings 的优先级是「init kwarg > 环境变量 > 字段默认值」，
    所以**没显式传**的字段（`llm_model` 等）会被宿主机配置顶掉，
    「出厂默认是什么」这类断言就变成「本机配了什么」。
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def _settings(**overrides) -> Settings:
    """构造一个「只剩 minimax 凭据」的配置（隔离宿主机环境变量）。"""
    base = dict(
        model_provider="minimax",
        minimax_api_key="mm-secret",
        dashscope_api_key="",
        deepseek_api_key="",
        embedding_provider="auto",
        rerank_provider="auto",
        ocr_provider="auto",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ---------- 装配 ----------


def test_build_llm_dispatch_minimax():
    """provider=minimax → MiniMaxLLM（**不是 MockLLM**）。"""
    llm = build_llm(
        provider="minimax",
        api_key="k",
        model="MiniMax-M3.1-Flash",
        temperature=0.0,
        max_retries=1,
        timeout_s=5.0,
        base_url="https://api.minimax.chat/v1",
    )
    assert isinstance(llm, MiniMaxLLM)
    assert not isinstance(llm, MockLLM)
    assert llm.model == "MiniMax-M3.1-Flash"


def test_build_llm_minimax_defaults():
    """不传 model / base_url 时用 MiniMax 默认值（不能落到千问默认）。"""
    llm = build_llm(
        provider="minimax", api_key="k", model="", temperature=0.0,
        max_retries=1, timeout_s=5.0, base_url="",
    )
    assert llm.model == "MiniMax-M3.1-Flash"
    assert llm._endpoint == "https://api.minimax.chat/v1/chat/completions"


# ---------- 配置解析 ----------


def test_resolved_provider_and_credentials():
    s = _settings()
    assert s.resolved_provider == "minimax"
    assert s.resolved_llm_api_key == "mm-secret"
    assert s.resolved_llm_base_url == "https://api.minimax.chat/v1"


def test_auto_prefers_deepseek_then_minimax_then_dashscope():
    """auto 的判定顺序：DeepSeek → MiniMax → 千问。"""
    assert Settings(
        _env_file=None, model_provider="auto", deepseek_api_key="d", minimax_api_key="m",
        dashscope_api_key="q",
    ).resolved_provider == "deepseek"
    assert Settings(
        _env_file=None, model_provider="auto", deepseek_api_key="", minimax_api_key="m",
        dashscope_api_key="q",
    ).resolved_provider == "minimax"
    assert Settings(
        _env_file=None, model_provider="auto", deepseek_api_key="", minimax_api_key="",
        dashscope_api_key="q",
    ).resolved_provider == "dashscope"
    assert Settings(
        _env_file=None, model_provider="auto", deepseek_api_key="", minimax_api_key="",
        dashscope_api_key="",
    ).resolved_provider == "mock"


def test_embedding_follows_minimax_because_embo01_exists():
    """向量**跟着 MiniMax 走**：它有 `embo-01`（实测 `POST /v1/embeddings` 可用）。

    这条曾经断言「回落 dashscope」，实测推翻：MiniMax 是有向量接口的，
    只是形状不是 OpenAI 的（`texts`/`type`，1536 维），见 `MiniMaxEmbeddings`。
    回落逻辑一旦写错，会把请求发到一个**存在的**端点却用错请求体 → 400 而非 404，
    更难定位。
    """
    s = _settings()
    assert s.resolved_embedding_provider == "minimax"
    assert s.resolved_embedding_model == "embo-01"
    assert s.resolved_embedding_dim == 1536
    assert s.resolved_embedding_api_key == "mm-secret"
    assert s.resolved_embedding_base_url == "https://api.minimax.chat/v1"


def test_rerank_and_ocr_still_fall_back_to_dashscope():
    """重排 / OCR 仍回落 dashscope（MiniMax 这两条实测 404）。"""
    s = _settings()
    assert s.resolved_rerank_provider == "dashscope"
    assert s.resolved_ocr_provider == "dashscope"


def test_rerank_can_explicitly_use_deepseek_chat_model():
    """显式 `RERANK_PROVIDER=deepseek` → 对话模型 listwise 重排，且模型名换成对话模型。

    `rerank_model` 的出厂默认是千问重排名 `gte-rerank-v2`，原样发给 DeepSeek 会 400，
    所以这里必须替换成对话模型（而不是把 gte- 的名字透传下去）。
    """
    s = _settings(model_provider="deepseek", deepseek_api_key="ds-secret",
                  rerank_provider="deepseek")
    assert s.resolved_rerank_provider == "deepseek"
    assert s.resolved_rerank_model == "deepseek-chat"
    assert s.resolved_rerank_api_key == "ds-secret"
    assert s.resolved_rerank_base_url == "https://api.deepseek.com"


def test_explicit_channel_provider_wins():
    """显式指定通道 provider 时不回落（回落只发生在 auto）。"""
    s = _settings(embedding_provider="dashscope", rerank_provider="mock")
    assert s.resolved_embedding_provider == "dashscope"
    assert s.resolved_rerank_provider == "mock"


# ---------- 模型名替换 ----------


def test_qwen_model_names_are_replaced_by_provider_defaults():
    s = _settings()
    assert s.llm_model == "qwen-plus"          # 出厂默认没动
    assert s.resolved_llm_model == "MiniMax-M3.1-Flash"
    assert s.resolved_intent_model == "MiniMax-M3.1-Flash"
    assert s.resolved_agent_planner_model == "MiniMax-M3"


def test_explicit_non_qwen_model_name_is_kept():
    """用户写了非千问模型名 → 原样使用（「配了却不生效」很难排查）。"""
    s = _settings(llm_model="MiniMax-M2.7", intent_model="MiniMax-M2.1")
    assert s.resolved_llm_model == "MiniMax-M2.7"
    assert s.resolved_intent_model == "MiniMax-M2.1"


def test_model_replacement_is_reported_in_notes():
    """替换必须留痕：否则「配置写 qwen-plus、实际发 MiniMax」对不上账。"""
    notes = "\n".join(_settings().provider_notes())
    assert "llm_model=qwen-plus" in notes
    assert "MiniMax-M3.1-Flash" in notes


def test_dashscope_provider_keeps_qwen_models():
    s = Settings(_env_file=None, model_provider="dashscope", dashscope_api_key="q")
    assert s.resolved_llm_model == "qwen-plus"
    assert s.resolved_intent_model == "qwen-turbo"
    assert s.provider_notes() == []


# ---------- 启动期校验 ----------


def test_validate_startup_requires_minimax_key():
    issues = _settings(minimax_api_key="").validate_startup()
    assert any(i["field"] == "minimax_api_key" for i in issues)


def test_validate_startup_rejects_illegal_provider_value():
    issues = _settings(model_provider="minimx").validate_startup()
    hit = [i for i in issues if i["field"] == "model_provider"]
    assert hit and "minimax" in hit[0]["hint"]


def test_validate_startup_rejects_bad_base_url():
    issues = _settings(minimax_base_url="api.minimax.chat/v1").validate_startup()
    assert any(i["field"] == "minimax_base_url" for i in issues)


def test_validate_startup_accepts_minimal_minimax_config():
    """只配 MINIMAX_API_KEY 就能启动（模型名会自动替换，不该报错）。"""
    assert _settings().validate_startup() == []


# ---------- 业务错误翻译（HTTP 200 里的错误） ----------


def _fake_stream(monkeypatch, lines):
    from app.services import openai_compat

    def _stream_lines(url, payload, api_key, timeout_s=30.0):
        for line in lines:
            yield line

    monkeypatch.setattr(openai_compat, "stream_lines", _stream_lines)


def _llm(max_retries: int = 1) -> MiniMaxLLM:
    return build_llm(
        provider="minimax", api_key="k", model="MiniMax-M3.1-Flash",
        temperature=0.0, max_retries=max_retries, timeout_s=5.0,
        base_url="https://api.minimax.chat/v1",
    )


def test_business_error_in_http_200_is_raised(monkeypatch):
    """`base_resp.status_code=1004`（鉴权失败）→ LLMError，不重试、不降级。"""
    _fake_stream(monkeypatch, [
        '{"base_resp":{"status_code":1004,"status_msg":"invalid api key"},"choices":[]}',
    ])
    with pytest.raises(LLMError) as exc:
        _llm(max_retries=3).generate("问题")
    assert exc.value.code == ErrCode.GENERATION_FAILED
    assert exc.value.retryable is False
    assert exc.value.degradable is False
    assert "1004" in str(exc.value)


def test_rate_limit_business_error_is_retryable(monkeypatch):
    """`1002`（触发限流）→ RATE_LIMITED 且可重试（与其他 provider 的 429 同口径）。"""
    _fake_stream(monkeypatch, [
        '{"base_resp":{"status_code":1002,"status_msg":"rate limit"}}',
    ])
    with pytest.raises(LLMError) as exc:
        _llm().generate("问题")
    assert exc.value.code == ErrCode.RATE_LIMITED
    assert exc.value.retryable is True


def test_insufficient_balance_is_not_retried(monkeypatch):
    """`1008`（余额不足）→ 确定性失败：重试三次只是把同一个拒绝问三遍。"""
    calls = {"n": 0}

    def _stream_lines(url, payload, api_key, timeout_s=30.0):
        calls["n"] += 1
        yield '{"base_resp":{"status_code":1008,"status_msg":"insufficient balance"}}'

    from app.services import openai_compat

    monkeypatch.setattr(openai_compat, "stream_lines", _stream_lines)
    with pytest.raises(LLMError) as exc:
        _llm(max_retries=3).generate("问题")
    assert calls["n"] == 1
    assert exc.value.retryable is False


def test_success_chunk_with_zero_status_is_transparent(monkeypatch):
    """`status_code=0` 正常放行：错误检查不能吃掉正常内容。"""
    _fake_stream(monkeypatch, [
        '{"base_resp":{"status_code":0,"status_msg":"success"},'
        '"choices":[{"delta":{"content":"答案"}}]}',
        "[DONE]",
    ])
    llm = _llm()
    assert llm.generate("问题") == "答案"
    assert llm.last_usage.output_tokens > 0


def test_streaming_yields_pieces(monkeypatch):
    _fake_stream(monkeypatch, [
        '{"choices":[{"delta":{"content":"甲"}}]}',
        '{"choices":[{"delta":{"content":"乙"}}]}',
        "[DONE]",
    ])
    assert list(_llm().stream("问题")) == ["甲", "乙"]


# ---------- 推理模型特性（M3.1-Flash 恒开思考） ----------


def test_reasoning_content_is_not_treated_as_answer(monkeypatch):
    """`reasoning_content` 是思考过程，不能混进答案正文。"""
    _fake_stream(monkeypatch, [
        '{"choices":[{"delta":{"reasoning_content":"先分析一下……"}}]}',
        '{"choices":[{"delta":{"content":"答案"}}]}',
        "[DONE]",
    ])
    assert _llm().generate("问题") == "答案"


def test_output_budget_adds_reasoning_headroom(monkeypatch):
    """`ST-04` 的输出上限要加推理余量：思考 token 也计入 max_tokens。"""
    seen: dict = {}

    def _stream_lines(url, payload, api_key, timeout_s=30.0):
        seen.update(payload)
        yield '{"choices":[{"delta":{"content":"答"}}]}'
        yield "[DONE]"

    from app.services import openai_compat

    monkeypatch.setattr(openai_compat, "stream_lines", _stream_lines)
    _llm().generate("问题", max_tokens=100)
    assert seen["max_tokens"] == 100 + MiniMaxLLM._REASONING_HEADROOM
    assert "max_completion_tokens" not in seen  # 不与 max_tokens 同时下发


def test_no_max_tokens_means_no_budget_field(monkeypatch):
    """没配输出上限时不下发 max_tokens（保持上游默认，别凭空加余量）。"""
    seen: dict = {}

    def _stream_lines(url, payload, api_key, timeout_s=30.0):
        seen.update(payload)
        yield '{"choices":[{"delta":{"content":"答"}}]}'

    from app.services import openai_compat

    monkeypatch.setattr(openai_compat, "stream_lines", _stream_lines)
    _llm().generate("问题")
    assert "max_tokens" not in seen


def test_empty_body_from_thinking_is_a_failure(monkeypatch):
    """思考烧完上限 → 空正文 + finish_reason=length，必须报错而不是返回空串。"""
    _fake_stream(monkeypatch, [
        '{"choices":[{"delta":{"reasoning_content":"想很久……"}}]}',
        '{"choices":[{"delta":{},"finish_reason":"length"}]}',
        "[DONE]",
    ])
    with pytest.raises(LLMError) as exc:
        _llm().generate("问题")
    assert exc.value.code == ErrCode.GENERATION_FAILED
    assert exc.value.retryable is False       # 同预算重试只是再烧一次思考
    assert "max_tokens" in str(exc.value)


def test_truncated_but_nonempty_answer_is_kept(monkeypatch):
    """有正文就算截断也返回（让人看到已生成的部分），截断只记警告。"""
    _fake_stream(monkeypatch, [
        '{"choices":[{"delta":{"content":"前半段"}}]}',
        '{"choices":[{"delta":{},"finish_reason":"length"}]}',
        "[DONE]",
    ])
    llm = _llm()
    assert llm.generate("问题") == "前半段"
    assert llm.last_finish_reason == "length"
