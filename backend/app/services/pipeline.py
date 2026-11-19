"""RAG 流水线：缓存 → 预处理 → 混合检索 → CRAG 校验 → 生成 → 幻觉自检。

# 注：阶段顺序是**缓存 → 预处理**（缓存查找在前、预处理在后）——此前模块头写成
# 「预处理 → 缓存」，与实现相反（`A-22.1`）。

- **校验发生在生成之前**：CRAG 先把证据筛干净（评分/精炼/重检索/拒答），
  模型只在证据合格后才被调用——从流程上排除"看到噪声证据后编造"的主要诱因；
- 拒答是一等公民：HTTP 200 + verdict=refused + refusal_reason（可归因），不走错误码；
- 降级是平行分支而非异常出口：评分器/自检器失效时请求继续走完，
  confidence 标为 None（未知）并如实提示"本次未做证据校验"；
- 受限重生成只一次：二次校验仍有无据声明 → 如实输出并下调置信度，不掩饰。
"""
import threading
from typing import Callable, Iterator

from app.core.config import Settings
from app.core.deadline import Deadline
from app.core.logging_setup import get_logger
from app.core.metrics import MetricsStore, PromMetrics
from app.domain.schemas import AskResult, Evidence, RetrievalConfig, SelfCheckRecord
from app.services.cache import SemanticCache
from app.services.config_store import RetrievalConfigStore
from app.services.llm import LLM
from app.services.retrieval.hybrid import HybridResult, HybridRetriever
from app.services.validation.crag import (
    CragRouter,
)
from app.services.validation.selfcheck import (
    HallucinationChecker,
)

logger = get_logger(__name__)

# 协议级常量（定义在 domain 层）：模型"检索不到"的措辞与对外拒答文案。
# 这里再导出一次，是为了让既有的 `from app.services.pipeline import NO_INFO_ANSWER`
# 继续可用（多处调用方在用）。
from app.domain.constants import (  # noqa: E402,F401
    NO_INFO_ANSWER,
    REFUSAL_REASON_UPSTREAM_UNAVAILABLE,
    REFUSAL_TEXT,
    UPSTREAM_UNAVAILABLE_TEXT,
)

# 提示词与证据装配已抽到 `services/generation/`（`B0 X-27` 第一步）。
# 这里**再导出一次**：`api` / `agent` / `context.partition` / 单测都在
# `from app.services.pipeline import build_evidence_block / GENERAL_KNOWLEDGE_PROMPT ...`，
# 搬走定义但保留导入路径，调用方零改动。
from app.services.generation import (  # noqa: E402
    cache_stage,
    core,
    generate_stage,
    guard_stage,
    retrieve_stage,
)
from app.services.generation.evidence import (  # noqa: E402,F401
    asset_uri as _asset_uri,
    build_candidates as _build_candidates,
    evidence_for_assembled as _evidence_for_assembled,
    rejected_evidence as _rejected_evidence,
)
from app.services.generation.prompt import (  # noqa: E402,F401
    EVIDENCE_BLOCK_CLOSE,
    EVIDENCE_BLOCK_OPEN,
    GENERAL_KNOWLEDGE_PROMPT,
    PROMPT_TEMPLATE,
    REASON_INSUFFICIENT,
    REASON_NO_CONTENT,
    build_evidence_block,
    build_prompt as _build_prompt,
)

# 阶段间共享类型与纯判定（`B0 X-27` 第二步）：
# 同样**再导出**，`api` / 单测里的 `from app.services.pipeline import PipelineCancelled`
# 与 `RagPipeline._to_self_check_record` 一类调用面保持零改动。
from app.services.generation.types import (  # noqa: E402,F401
    PipelineCancelled,
    is_upstream_unavailable as _is_upstream_unavailable,
    route_degraded as _route_degraded,
    route_model as _route_model,
)


class RagPipeline:
    def __init__(
        self,
        settings: Settings,
        llm: LLM,
        hybrid: HybridRetriever,
        cache: SemanticCache,
        config_store: RetrievalConfigStore,
        prom: PromMetrics,
        metrics_store: MetricsStore,
        doc_name_resolver: Callable[[str], str] | None = None,
        docstore=None,
        index_version_provider: Callable[[], int] | None = None,
        preprocessor=None,
        analysis_store=None,
        crag_router: CragRouter | None = None,
        self_checker: HallucinationChecker | None = None,
        history_store=None,  # ValidationHistoryStore：CRAG 轮次 / 自检明细落库
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._hybrid = hybrid
        self._cache = cache
        self._config_store = config_store
        self._prom = prom
        self._metrics_store = metrics_store
        self._doc_name_resolver = doc_name_resolver or (lambda doc_id: doc_id)
        self._docstore = docstore
        self._index_version = index_version_provider or (lambda: 0)
        self._preprocessor = preprocessor
        self._analysis_store = analysis_store
        self._crag = crag_router
        self._self_checker = self_checker
        self._history = history_store
        # 本轮被上下文组装丢弃的候选项（前端据此解释"为什么没进上下文"）
        # 被丢弃的候选（`A-21.7`）：随 `_final` 进响应，供前端解释
        # 「为什么这条证据没进上下文」。此前只写不读。
        self._context_dropped: list[dict] = []
        # `ST-01` 阶段 A：上下文构成表（空 = 未装配）
        self._context_breakdown: dict = {}
        # 批次 2 single-flight：相同 cache key 的并发请求合并为一次真实计算，
        # 等待者带 deadline 复用结果（bind_principal 的副本共享同一 dict 引用）
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()

    # ---- 护栏挂载点（同一请求内固定，便于审计三事件复用同一主体）----

    def bind_principal(
        self, tenant_id: str, user_id: str = "anon", principal=None
    ) -> "RagPipeline":
        """绑定请求主体（副本语义，不改共享实例——流水线是单例）。

        认证已下线后主体恒为固定值，但**保留副本语义**：灰度配置仍按主体解析
        （租户定向 > 百分比分桶 > 全局），且未来若要恢复多租户，这里是唯一挂载点。
        """
        clone = object.__new__(RagPipeline)
        clone.__dict__.update(self.__dict__)
        clone._tenant_id = tenant_id
        clone._user_id = user_id
        # 灰度（批次 6）：按主体解析作用域配置（租户定向 > 百分比分桶 > 全局）。
        # 实验标签只进日志与响应，**不进指标 label**（高基数）。
        clone._experiment_label = ""
        try:
            store = getattr(self, "_config_store", None)
            resolver = getattr(store, "resolve", None)
            if callable(resolver):
                _cfg, clone._experiment_label = resolver(tenant_id, user_id)
        except Exception:  # noqa: BLE001 - 灰度解析失败不该让请求失败
            logger.debug("灰度解析失败不该让请求失败")
            clone._experiment_label = ""
        return clone

    def _scoped_config(self):
        """当前主体应使用的检索配置（灰度作用域优先于全局）。"""
        store = getattr(self, "_config_store", None)
        tenant = getattr(self, "_tenant_id", "") or ""
        user = getattr(self, "_user_id", "") or ""
        resolver = getattr(store, "resolve", None)
        if callable(resolver):
            try:
                config, label = resolver(tenant, user)
                self._experiment_label = label
                return config
            except Exception:  # noqa: BLE001 - 灰度解析失败回落全局
                logger.warning("灰度配置解析失败，回落全局配置", exc_info=True)
        return store.get_active() if store is not None else RetrievalConfig()

    def attach_history(self, history_store=None) -> None:
        """装配期回填 校验历史存储。

        校验历史（CRAG 轮次 / 自检明细）在装配顺序上晚于流水线创建，
        因此不能在构造参数里传——否则只能挂到 Agent 路径上，线性路径永远拿不到。
        """
        self._history = history_store

    @property
    def _principal_tenant(self) -> str:
        return getattr(self, "_tenant_id", "")

    @property
    def _principal_user(self) -> str:
        return getattr(self, "_user_id", "anon")

    # ---- 缓存键 / 读写（实现见 `generation/cache_stage.py`，`B0 X-27` 第二步）----
    # 保留同名薄方法，是因为已有三处依赖这些名字：单测直接调 `pipeline._cache_key(...)`、
    # `_cache_lookup` 被 `_core_events` 调用、子类可能 override。
    # 真正的实现放在阶段模块里，首参传 `pip`（请求级副本）——见 `cache_stage` 模块文档。

    def _model_version(self, config: RetrievalConfig) -> str:
        return cache_stage.model_version(self, config)

    def _cache_dims(self, config: RetrievalConfig) -> tuple[str, str]:
        return cache_stage.cache_dims(self, config)

    def _cache_key(
        self, question: str, tenant_id: str, config: RetrievalConfig, doc_id: str | None = None
    ) -> str:
        return cache_stage.cache_key(self, question, tenant_id, config, doc_id)

    def _question_vector(self, question: str) -> list:
        return cache_stage.question_vector(self, question)

    def _cache_lookup(
        self, question: str, tenant_id: str, key: str, scope: str = "", signature: str = ""
    ):
        return cache_stage.cache_lookup(self, question, tenant_id, key, scope, signature)

    def _cache_store(
        self, key: str, question: str, payload: dict, ttl: float,
        tenant_id: str = "", scope: str = "", signature: str = "",
    ) -> None:
        cache_stage.cache_store(
            self, key, question, payload, ttl, tenant_id, scope, signature
        )

    # ---- 预处理 / 过滤条件（实现见 `generation/retrieve_stage.py`）----
    # `_degrade_level` / `_build_filters` 保留方法名：前者被单测直接定型（三档水位），
    # 后者被子类与 `/ask` 快问路径按名调用。

    def _degrade_level(self) -> int:
        return retrieve_stage.degrade_level(self)

    def _preprocess(
        self, question: str, tenant_id: str
    ) -> tuple[dict | None, list[tuple[str, str]], bool]:
        return retrieve_stage.preprocess(self, question, tenant_id)

    def _build_filters(self, tenant_id: str, doc_id: str | None = None):
        return retrieve_stage.build_filters(self, tenant_id, doc_id)

    # ---- 请求级缓存（批次 2 / KQ-12）：同一请求内消除重复查询 ----

    def _rcache(self) -> dict:
        """请求级缓存（bind_principal 的副本天然按请求隔离，无需 contextvar）。"""
        cache = getattr(self, "_req_cache", None)
        if cache is None:
            cache = {"parents": {}, "names": {}}
            self._req_cache = cache
        return cache

    def _reset_rcache(self) -> None:
        self._req_cache = None

    def _parent_of(self, parent_id: str):
        cache = self._rcache()["parents"]
        if parent_id not in cache:
            cache[parent_id] = self._docstore.get_parent(parent_id)
        return cache[parent_id]

    def _name_of(self, doc_id: str) -> str:
        cache = self._rcache()["names"]
        if doc_id not in cache:
            cache[doc_id] = self._doc_name_resolver(doc_id)
        return cache[doc_id]

    # ---- 生成段（实现见 `generation/generate_stage.py`）----
    # 保留同名方法：`_llm_generate` 被单测替换 / 断言，`_finalize_streamed_answer`
    # 被契约测试用 `__get__(holder)` 绑定到伪对象后直接调用。

    def _llm_generate(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> str:
        return generate_stage.llm_generate(self, prompt, deadline, max_tokens)

    def _llm_stream(
        self, prompt: str, deadline: "Deadline | None" = None,
    ) -> Iterator[str]:
        return generate_stage.llm_stream(self, prompt, deadline)

    def _finalize_streamed_answer(
        self,
        question: str,
        tenant_id: str,
        trace_id: str,
        analysis,
        hybrid_result,
        crag_payload,
        answer: str,
        used_evidence,
        evidence_block: str,
        llm_calls: int,
        stages: dict[str, int],
        cancel: "threading.Event | None" = None,
        evidence=None,
    ) -> Iterator[tuple[str, dict]]:
        return generate_stage.finalize_streamed_answer(
            self, question, tenant_id, trace_id, analysis, hybrid_result, crag_payload,
            answer, used_evidence, evidence_block, llm_calls, stages, cancel, evidence,
        )

    def _llm_generate_or_degrade(self, prompt: str, deadline: "Deadline | None" = None) -> str | None:
        return generate_stage.generate_or_degrade(self, prompt, deadline)

    def _degraded_events(
        self,
        *,
        trace_id: str,
        analysis: dict | None,
        hybrid_result,
        crag_payload,
        llm_calls: int,
    ) -> Iterator[tuple[str, dict]]:
        return generate_stage.degraded_events(
            self,
            trace_id=trace_id, analysis=analysis, hybrid_result=hybrid_result,
            crag_payload=crag_payload, llm_calls=llm_calls,
        )

    def _parametric_fallback_events(
        self, question: str, tenant_id: str, trace_id: str, analysis: dict | None,
        hybrid_result, llm_calls: int, crag_payload: dict | None,
        refusal_reason: str, deadline: "Deadline | None" = None,
    ) -> Iterator[tuple[str, dict]]:
        return generate_stage.parametric_fallback_events(
            self, question, tenant_id, trace_id, analysis, hybrid_result, llm_calls,
            crag_payload, refusal_reason, deadline,
        )

    def _to_evidence(self, hybrid_result: HybridResult) -> list[Evidence]:
        """装配证据（实现见 `generation/retrieve_stage.to_evidence`）。

        这里只做代理：装配逻辑依赖「设置 + 文档名解析 + 父块展开」，全部经由
        `self` 的请求级副本传入，因此方法名与调用面不变。
        """
        return retrieve_stage.to_evidence(self, hybrid_result)

    def build_prompt(self, question: str, evidence: list[Evidence]) -> str:
        """渲染问答提示词（`ST-01` 阶段 B：按配置选择 legacy / structured）。

        `structured=False` 时与 `generation/prompt.build_prompt` **逐字节一致**，
        因此默认配置下的行为与阶段 A 完全相同；切换只发生在一行配置上。
        线性路径没有记忆 / 历史 / 工具目录三个来源（那三个只在 Agent 路径存在），
        所以这里只可能把「证据段」渲染成带标题的分区段。
        """
        mode = str(getattr(self._settings, "context_partition_render", "legacy") or "legacy")
        if mode != "structured":
            return _build_prompt(question, evidence)
        # 按需载入：默认路径不必背 render 模块的导入成本
        from app.services.context.render import render_prompt

        return render_prompt(question, evidence, structured=True)

    # ---- 指标 / 编排（实现见 `generation/core.py`）----
    # 保留同名方法：`_record` 被取消语义的测试用 monkeypatch 替换，
    # `_core_events` / `_assemble` 被 `ask` 与契约测试按名引用。

    def _record(
        self,
        question: str,
        tenant_id: str,
        ok: bool,
        latency_ms: float,
        from_cache: bool,
        llm_called: bool,
        path: str = "ask",
    ) -> None:
        core.record(self, question, tenant_id, ok, latency_ms, from_cache, llm_called, path)

    def _build_context_breakdown(self, assembled) -> dict:
        """上下文构成表（`ST-01` 阶段 A，实现见 `generation/retrieve_stage`）。"""
        return retrieve_stage.build_context_breakdown(self, assembled)

    def _observe_retrieval(self, hybrid_result: HybridResult) -> None:
        """检索阶段观测（实现见 `generation/retrieve_stage.observe_retrieval`）。"""
        retrieve_stage.observe_retrieval(self, hybrid_result)

    # ----  核心：CRAG 校验 → 生成 → 自检（事件流）----

    def _core_events(
        self, question: str, tenant_id: str, trace_id: str, config: RetrievalConfig,
        doc_id: str | None = None, deadline: "Deadline | None" = None,
        stages: dict[str, int] | None = None,
        stream_tokens: bool = False, cancel: "threading.Event | None" = None,
    ) -> Iterator[tuple[str, dict]]:
        """校验闭环主流程（实现见 `generation/core.core_events`）。

        事件同时支撑 `/ask`（聚合）与 `/ask/stream`（透传）——两端口共用同一条
        事件流，是「阻塞答案与流式答案不会漂移」的结构性保证。
        """
        return core.core_events(
            self, question, tenant_id, trace_id, config, doc_id, deadline,
            stages, stream_tokens, cancel,
        )

    def _generate_followups(self, answer: str) -> list[str]:
        """推荐追问（实现见 `generation/generate_stage.generate_followups`）。"""
        return generate_stage.generate_followups(self, answer)

    # ---- 阶段 5 历史落库 ----

    @staticmethod
    def _to_self_check_record(
        checker, result, answer: str, removed: int, remaining: int
    ) -> SelfCheckRecord:
        """自检明细记录（实现见 `generation/guard_stage`）。"""
        return guard_stage.to_self_check_record(checker, result, answer, removed, remaining)

    def _persist_round(self, trace_id: str, question: str, tenant_id: str, round_data: dict) -> None:
        """CRAG 单轮落库（实现见 `generation/core.persist_round`）。"""
        core.persist_round(self, trace_id, question, tenant_id, round_data)

    def _persist_self_check(self, trace_id: str, tenant_id: str, record) -> None:
        """自检记录落库（实现见 `generation/guard_stage`）。"""
        guard_stage.persist_self_check(self, trace_id, tenant_id, record)

    # ---- 阶段耗时汇总（`A-17`）----

    @staticmethod
    def _log_stage_summary(trace_id: str, stages: dict[str, int], total_ms: int) -> None:
        core.log_stage_summary(trace_id, stages, total_ms)

    def _assemble(self, trace_id: str, started: float, events: list[tuple[str, dict]]) -> AskResult:
        """事件聚合（实现见 `generation/core.assemble`）。"""
        return core.assemble(self, trace_id, started, events)

    def _record_quality(self, result: AskResult) -> None:
        core.record_quality(self, result)

    # ---- single-flight（批次 2）：相同问题的并发请求合并为一次真实计算 ----
    # 实现见 `generation/cache_stage.py`；`_inflight` / `_inflight_lock` 仍在本实例上，
    # 因为它们是**进程级共享**的（`bind_principal` 有意共享同一个 dict 引用）。

    _SF_WAIT_S = cache_stage.SF_WAIT_S

    def _sf_acquire(self, cache_key: str) -> tuple[threading.Event, bool]:
        return cache_stage.sf_acquire(self, cache_key)

    def _sf_release(self, cache_key: str, ev: threading.Event) -> None:
        cache_stage.sf_release(self, cache_key, ev)

    def _cached_result(self, entry, hit_kind: str, question: str, tenant_id: str,
                       trace_id: str, started: float) -> AskResult:
        """缓存命中的聚合返回（实现见 `generation/core.cached_result`）。"""
        return core.cached_result(self, entry, hit_kind, question, tenant_id, trace_id, started)

    # ---- 同步问答 ----

    def ask(
        self, question: str, tenant_id: str, trace_id: str, top_k: int | None = None,
        doc_id: str | None = None, deadline: "Deadline | None" = None,
        cancel: "threading.Event | None" = None,
    ) -> AskResult:
        """同步问答（实现见 `generation/core.ask`）。

        `cancel`（`C-04`）：阻塞端点同样要能被打断，取消时抛 `PipelineCancelled`，
        由端点层静默收尾；半截结果**不写缓存**。
        """
        return core.ask(
            self, question, tenant_id, trace_id, top_k, doc_id, deadline, cancel
        )

    # ---- 流式问答 ----

    def _cached_stream_events(self, entry, hit_kind: str, question: str, tenant_id: str,
                              trace_id: str, started: float) -> Iterator[tuple[str, dict]]:
        """缓存命中（含 single-flight 等待者）的事件重放。"""
        return core.cached_stream_events(
            self, entry, hit_kind, question, tenant_id, trace_id, started
        )

    def ask_stream(
        self, question: str, tenant_id: str, trace_id: str, top_k: int | None = None,
        doc_id: str | None = None, deadline: "Deadline | None" = None,
        cancel: "threading.Event | None" = None,
    ) -> Iterator[tuple[str, dict]]:
        """流式问答（实现见 `generation/core.ask_stream`）。

        `cancel`（`C-04`）：客户端断开时置位；管线在阶段边界与生成循环里检查它，
        **提前收尾**（不等模型把话说完）。
        """
        return core.ask_stream(
            self, question, tenant_id, trace_id, top_k, doc_id, deadline, cancel
        )


# `_asset_uri` 已移到 `services/generation/evidence.py`（`B0 X-27` 第一步），
# 并在模块顶部以同名再导出：`tests/unit/test_image_text_binding.py` 仍从本模块导入。

