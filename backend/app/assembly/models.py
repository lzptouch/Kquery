"""模型装配：生成 / 向量 / 重排 / OCR / ASR（`A-12`）。

## 三个 provider 各自独立

**生成**（`resolved_provider`）、**向量**（`resolved_embedding_provider`）、
**重排**（`resolved_rerank_provider`）是三条独立通道：DeepSeek / MiniMax 都只有对话补全，
向量与重排仍走千问或 OpenAI 兼容服务。回落规则在 `core/config.py`，
这里只按解析结果装配——装配层不做二次判断，否则回落逻辑就有两份真相。

## 四个 LLM 档位

| 档位 | 用途 | 默认模型 |
|---|---|---|
| `llm` | 问答生成 / 规划回退 | `resolved_llm_model` |
| `fast_llm` | 意图识别（最低档） | `resolved_intent_model` |
| `agent_llm` | Agent 规划 / 图谱抽取 | `resolved_agent_planner_model` |
| `llm`（降级链） | 主模型失败时换模型 | `llm_fallback_models` |

模型名一律用 `resolved_*` 版本：出厂默认是千问模型名，换成 deepseek / minimax
后由 `core/config.py` 的 `PROVIDER_MODEL_DEFAULTS` 替换（否则首次请求就 404）。

降级链是**包一层**（`LLMRouter`）而不是换模型：候选链失败仍回主模型的错误，
因此下游看到的异常语义不变。
"""
from __future__ import annotations

from app.assembly.context import Assembly
from app.core.logging_setup import get_logger
from app.services.embeddings import build_embeddings
from app.services.llm import build_llm
from app.services.parsers.ocr import build_ocr_engine
from app.services.retrieval.rerank import build_reranker

logger = get_logger(__name__)


def build_models(asm: Assembly) -> None:
    """按解析后的 provider 装好四条模型通道（生成 / 向量 / 重排 / 多模态解析）。"""
    settings = asm.settings
    app = asm.app
    provider = settings.resolved_provider

    # 模型层：**三个 provider 各自独立**（生成 / 向量 / 重排）——
    # DeepSeek 只有对话补全，向量与重排仍走千问或 OpenAI 兼容服务
    # （`config.resolved_*_provider` 负责回落，见 `core/config.py` 的说明）。
    embeddings = build_embeddings(
        provider=settings.resolved_embedding_provider,
        api_key=settings.resolved_embedding_api_key,
        model=settings.resolved_embedding_model,
        dim=settings.resolved_embedding_dim,
        max_retries=settings.llm_max_retries,
        base_url=settings.resolved_embedding_base_url,
    )
    llm = build_llm(
        provider=provider,
        api_key=settings.resolved_llm_api_key,
        model=settings.resolved_llm_model,
        temperature=settings.temperature,
        max_retries=settings.llm_max_retries,
        timeout_s=settings.llm_timeout_s,
        base_url=settings.resolved_llm_base_url,
        thinking_enabled=settings.zhipu_thinking_enabled,
    )
    # 模型路由与降级（`LM-01`）：**默认关**。开启后主模型失败且**可降级**
    # （超时 / 限流 / 5xx；鉴权与参数错不可降级）时按候选链换模型，
    # 并在响应里透出「实际使用的模型 + 是否降级」。
    if settings.llm_fallback_enabled and settings.llm_fallback_models.strip():
        # 按需载入：路由器只在开启降级时装配（默认关时不必付导入成本）
        from app.services.llm_router import LLMRouter, ModelPolicy

        _fallbacks = tuple(
            m.strip() for m in settings.llm_fallback_models.split(",") if m.strip()
        )
        _policy = ModelPolicy(primary=settings.resolved_llm_model, fallbacks=_fallbacks)

        def _llm_factory(model_name: str):
            return build_llm(
                provider=provider,
                api_key=settings.resolved_llm_api_key,
                model=model_name,
                temperature=settings.temperature,
                max_retries=settings.llm_max_retries,
                timeout_s=settings.llm_timeout_s,
                base_url=settings.resolved_llm_base_url,
                thinking_enabled=settings.zhipu_thinking_enabled,
            )

        llm = LLMRouter(
            _llm_factory,
            _policy,
            on_degrade=lambda reason: asm.prom.llm_degrade_total.labels(reason=reason).inc(),
        )
        logger.info(
            "模型路由已启用：主=%s，备选=%s", settings.resolved_llm_model, ",".join(_fallbacks)
        )
    reranker = build_reranker(
        provider=settings.resolved_rerank_provider,
        api_key=settings.resolved_rerank_api_key,
        model=settings.resolved_rerank_model,
        max_retries=settings.llm_max_retries,
        base_url=settings.resolved_rerank_base_url,
    )

    # Query 预处理（意图识别压到最低档）；Agent 规划单独一档（temperature=0）
    fast_llm = build_llm(
        provider=provider,
        api_key=settings.resolved_llm_api_key,
        model=settings.resolved_intent_model,
        temperature=settings.temperature,
        max_retries=settings.llm_max_retries,
        timeout_s=settings.llm_timeout_s,
        base_url=settings.resolved_llm_base_url,
        thinking_enabled=settings.zhipu_thinking_enabled,
    )
    agent_llm = build_llm(
        provider=provider,
        api_key=settings.resolved_llm_api_key,
        model=settings.resolved_agent_planner_model,
        temperature=0,
        max_retries=settings.llm_max_retries,
        timeout_s=settings.llm_timeout_s,
        base_url=settings.resolved_llm_base_url,
        thinking_enabled=settings.zhipu_thinking_enabled,
    )

    # OCR 一律云服务 API（见 parsers/ocr.py 的架构约束）；provider 单独解析，
    # 不用全局 provider：全局可能是 mock（测试），而 OCR 可以单独强制云端。
    if settings.resolved_ocr_provider == "zhipu":
        ocr_engine = build_ocr_engine(
            "zhipu",
            settings.zhipu_api_key,
            settings.llm_max_retries,
            base_url=settings.zhipu_base_url,
            model=settings.ocr_zhipu_model,
        )
    else:
        ocr_engine = build_ocr_engine(
            settings.resolved_ocr_provider, settings.dashscope_api_key, settings.llm_max_retries
        )

    asm.embeddings = embeddings
    asm.llm = llm
    asm.reranker = reranker
    asm.fast_llm = fast_llm
    asm.agent_llm = agent_llm
    asm.ocr_engine = ocr_engine
    app.state.embeddings = embeddings
    app.state.llm = llm
    app.state.reranker = reranker
