"""智谱（bigmodel.cn）provider：能力边界、思考开关、三通道装配。

## 为什么单独一个文件

智谱与 DeepSeek 的**传输层完全同形**（OpenAI 兼容 `/chat/completions`），
所以它继承 `DeepSeekLLM`，重试 / 超时 / deadline / usage 口径不需要重测。
真正需要钉住的是**三件容易静默出错的事**：

1. **glm-4.5 系默认开思考**：思考 token 计入 `max_tokens`，预算小时正文为空、
   `finish_reason=length`——RAG 会拿到「空答案但不报错」。默认必须关。
2. **三通道齐全**（chat / embeddings / rerank），所以向量与重排在 `auto` 下
   **跟着走、不回落 dashscope**；但 **OCR 仍要回落**（`glm-4.5v` 是对话式
   多模态，不是 `/ocr` 协议形状，强行复用会丢表格结构）。
3. **维度是 2048**（`embedding-3`），与 `embo-01` 的 1536 不同：
   换 provider 必须重建集合，配置层要把它顶掉而不是沿用千问默认的 1024。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.embeddings import OpenAICompatibleEmbeddings, build_embeddings
from app.services.llm import DeepSeekLLM, MockLLM, ZhipuLLM, build_llm
from app.services.retrieval.rerank import OpenAICompatibleReranker, build_reranker

# ---------- 夹具 ----------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """隔离宿主机环境变量。

    `Settings(_env_file=None)` 关不掉 `os.environ`：`pymilvus` 在 import 时
    `load_dotenv()` 会把 `backend/.env` 整份注入环境（本机写着
    `MODEL_PROVIDER=deepseek`），不清理则「出厂默认」的断言会变成「本机配了什么」。
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def _settings(**overrides) -> Settings:
    base = dict(
        model_provider="zhipu",
        zhipu_api_key="zp-secret",
        dashscope_api_key="",
        deepseek_api_key="",
        minimax_api_key="",
        embedding_provider="auto",
        rerank_provider="auto",
        ocr_provider="auto",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ---------- 生成：思考开关 ----------


def test_zhipu_llm_disables_thinking_by_default():
    """默认必须下发 `thinking: disabled`——否则 glm-4.5 系正文为空。"""
    llm = ZhipuLLM(api_key="k", model="glm-4.6")
    assert llm._extra_payload() == {"thinking": {"type": "disabled"}}


def test_zhipu_llm_can_enable_thinking_explicitly():
    llm = ZhipuLLM(api_key="k", model="glm-4.6", thinking_enabled=True)
    assert llm._extra_payload() == {"thinking": {"type": "enabled"}}


def test_zhipu_llm_inherits_deepseek_transport():
    """传输层继承而非复制：换模型不该换重试 / 超时行为。"""
    llm = ZhipuLLM(api_key="k")
    assert isinstance(llm, DeepSeekLLM)
    assert llm._endpoint.endswith("/chat/completions")


def test_deepseek_has_no_extra_payload():
    """基类默认不额外下发字段（避免把智谱专有键漏给 DeepSeek）。"""
    assert DeepSeekLLM(api_key="k")._extra_payload() == {}


# ---------- 装配：生成 ----------


def test_build_llm_dispatch_zhipu():
    llm = build_llm(
        provider="zhipu",
        api_key="k",
        model="glm-4.6",
        temperature=0.0,
        max_retries=1,
        timeout_s=5.0,
        base_url="https://open.bigmodel.cn/api/paas/v4",
    )
    assert isinstance(llm, ZhipuLLM)
    assert not isinstance(llm, MockLLM)


def test_build_llm_zhipu_passes_thinking_flag():
    llm = build_llm(
        provider="zhipu",
        api_key="k",
        model="glm-4.6",
        temperature=0.0,
        max_retries=1,
        timeout_s=5.0,
        thinking_enabled=True,
    )
    assert llm._extra_payload() == {"thinking": {"type": "enabled"}}


def test_build_llm_zhipu_defaults():
    """不传 model / base_url 时落到智谱默认，不能落到千问或 DeepSeek。"""
    llm = build_llm(
        provider="zhipu",
        api_key="k",
        model="",
        temperature=0.0,
        max_retries=1,
        timeout_s=5.0,
    )
    assert llm.model == "glm-4.6"
    assert "bigmodel.cn" in llm._base_url


# ---------- 解析：auto 顺序与模型名 ----------


def test_auto_prefers_zhipu_before_dashscope():
    """只有智谱 key 时，生成走智谱（不是因为千问 key 存在才走千问）。"""
    s = _settings(model_provider="auto")
    assert s.resolved_provider == "zhipu"


def test_explicit_zhipu_requires_key():
    s = _settings(zhipu_api_key="")
    errors = s.validate_startup()
    assert any(e["field"] == "zhipu_api_key" for e in errors)


def test_zhipu_model_names_are_replaced_from_qwen_defaults():
    """出厂模型名是千问名，切到智谱必须替换，否则首次请求 404。"""
    s = _settings()
    assert s.resolved_llm_model.startswith("glm-")
    assert s.resolved_intent_model.startswith("glm-")
    assert s.resolved_agent_planner_model.startswith("glm-")


def test_zhipu_notes_explain_thinking_switch():
    s = _settings()
    notes = " ".join(s.provider_notes())
    assert "思考" in notes


# ---------- 三通道：向量 / 重排跟随，OCR 回落 ----------


def test_embedding_follows_zhipu_because_embedding3_exists():
    s = _settings()
    assert s.resolved_embedding_provider == "zhipu"
    assert s.resolved_embedding_model == "embedding-3"


def test_embedding_dim_defaults_to_2048_not_qwen_1024():
    """千问出厂 1024 必须被顶掉，否则入库时报维度不符。"""
    s = _settings(embedding_dim=1024)
    assert s.resolved_embedding_dim == 2048


def test_rerank_follows_zhipu_because_rerank_endpoint_exists():
    s = _settings()
    assert s.resolved_rerank_provider == "zhipu"
    assert s.resolved_rerank_model == "rerank"
    assert "bigmodel.cn" in s.resolved_rerank_base_url


def test_ocr_falls_back_to_dashscope_for_zhipu():
    """`glm-4.5v` 是对话式多模态，不是 `/ocr` 协议：OCR 必须回落。"""
    s = _settings()
    assert s.resolved_ocr_provider == "dashscope"


def test_explicit_embedding_provider_zhipu_validates_dim():
    s = _settings(embedding_provider="zhipu", embedding_dim=1536)
    errors = s.validate_startup()
    assert any(e["field"] == "embedding_dim" for e in errors)


def test_explicit_rerank_provider_zhipu_requires_key():
    s = _settings(rerank_provider="zhipu", zhipu_api_key="", rerank_api_key="")
    errors = s.validate_startup()
    assert any(e["field"] == "zhipu_api_key" for e in errors)


# ---------- 装配：向量 / 重排（复用 OpenAI 兼容实现） ----------


def test_build_embeddings_zhipu_reuses_openai_compatible():
    """智谱 embeddings 是 OpenAI 形状：不该新增一套重复实现。"""
    emb = build_embeddings(
        provider="zhipu",
        api_key="k",
        model="embedding-3",
        dim=2048,
        max_retries=1,
        base_url="https://open.bigmodel.cn/api/paas/v4",
    )
    assert isinstance(emb, OpenAICompatibleEmbeddings)
    assert emb.dim == 2048


def test_build_reranker_zhipu_reuses_openai_compatible():
    """`/rerank` 响应 `results[].relevance_score` 与兼容实现同形。"""
    rr = build_reranker(
        provider="zhipu",
        api_key="k",
        model="rerank",
        max_retries=1,
        base_url="https://open.bigmodel.cn/api/paas/v4",
    )
    assert isinstance(rr, OpenAICompatibleReranker)
    assert "bigmodel.cn" in rr._base_url
