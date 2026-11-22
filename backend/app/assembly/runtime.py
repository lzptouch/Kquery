"""运行时装配：进程参数 / 指标 / 限流 / 缓存 / 执行器（`A-12`）。

## 装配顺序为什么在这些地方较真

- **指标必须最先**：模型层（`LLMRouter` 的降级计数）、流水线、中间件都要往里记数；
- **执行器必须早于流水线与 Agent**：限流拒绝（`C-11`）要靠执行器的队列深度。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from app.assembly.context import Assembly
from app.core.logging_setup import get_logger
from app.core.metrics import MetricsStore, PromMetrics
from app.services.cache import build_cache
from app.services.config_store import RetrievalConfigStore
from app.services.idempotency import IdempotencyStore
from app.services.metrics_query import PrometheusClient
from app.services.pipeline_executor import PipelineExecutor
from app.services.ratelimit import build_limiter
from app.services.sse_replay import SseReplayStore
from app.services.upstream import bulkheads_stats, configure_bulkheads

logger = get_logger(__name__)


def _build_canary_controller(settings, config_store, metrics_store):
    """装配灰度自动回滚控制器（`OP-02`）。

    ## 指标来源的选择

    - **配置了 `prometheus_url`** → 用 Prometheus 查询（多副本可比）；
    - **未配置** → 用进程内指标（**仅单副本可比**）。此时若服务是多副本，
      判定会基于不完整的样本——所以注释与文档都写明「多副本必须配 Prometheus」。

    ## 规则从配置解析

    规则是 JSON 数组；**解析失败不静默**——宁可「没有规则」（什么都不做），
    也不要拿解析失败的规则去回滚灰度。
    """
    # 按需载入：灰度控制器只在装配期用一次（不必进装配层的模块级依赖）
    from app.services.canary import (
        CanaryController,
        CanaryRule,
        InProcessMetricSource,
        MultiReplicaMetricSource,
    )

    rules: list[CanaryRule] = []
    raw = str(getattr(settings, "canary_auto_rollback_rules", "") or "").strip()
    if raw:
        try:
            for item in json.loads(raw):
                rules.append(
                    CanaryRule(
                        metric=str(item.get("metric") or ""),
                        direction=str(item.get("direction") or "gt"),
                        threshold=float(item.get("threshold") or 0.0),
                        relative=bool(item.get("relative", False)),
                        min_samples=int(item.get("min_samples") or 30),
                        observe_minutes=int(item.get("observe_minutes") or 5),
                    )
                )
        except Exception as exc:  # noqa: BLE001 - 规则解析失败要看得见
            logger.error("灰度自动回滚规则解析失败（本次不做任何判定）: %s", exc)
            rules = []

    prometheus_url = str(getattr(settings, "prometheus_url", "") or "").strip()
    metric_source = (
        MultiReplicaMetricSource(prometheus_url)
        if prometheus_url
        else InProcessMetricSource(metrics_store)
    )
    return CanaryController(
        config_store=config_store,
        metric_source=metric_source,
        rules=rules,
        enabled=bool(getattr(settings, "canary_auto_rollback_enabled", False)),
    )


def prepare_process(asm: Assembly) -> None:
    """进程级参数：AnyIO 默认线程池与上游隔离仓。

    必须在 lifespan 内设置：导入期没有事件循环，调 limiter 会抛 `NoEventLoopError`。
    """
    settings = asm.settings
    # 批次 1：AnyIO 默认线程池（40）留给管理面/探针，问答路径由 PipelineExecutor
    # 独立承载——共享池调到 100，避免管理端点在问答高峰期排队。
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = 100
    # 批次 2：上游隔离仓与熔断（许可之和 ≤ pipeline_workers）
    configure_bulkheads(settings)


def build_metrics(asm: Assembly) -> None:
    """全局指标对象与进程内指标存储（**第一个**装配域：其他层都要往里记数）。"""
    prom = PromMetrics()
    metrics_store = MetricsStore()
    asm.prom = prom
    asm.metrics_store = metrics_store
    asm.app.state.prom = prom
    asm.app.state.metrics_store = metrics_store


def build_runtime(asm: Assembly) -> None:
    """配置存储 / 幂等 / 缓存 / 限流 / 执行器 / 灰度控制器。"""
    settings = asm.settings
    app = asm.app
    prom = asm.prom
    metrics_store = asm.metrics_store

    config_store = RetrievalConfigStore(
        settings.data_dir / "retrieval-config.json",
        apply_delay_s=settings.retrieval_config_apply_delay_s,
        settings=settings,
    )
    asm.config_store = config_store
    app.state.config_store = config_store
    # 幂等（在飞去重 + 结果重放）：进程内存储（单副本语义）
    app.state.idempotency = IdempotencyStore()
    cache = build_cache(
        settings=settings,
        embeddings=asm.embeddings,
        tenant_id=settings.default_tenant_id,
    )
    asm.cache = cache
    app.state.cache = cache
    limiter = build_limiter()
    asm.limiter = limiter
    app.state.limiter = limiter
    # 多副本下的指标真相源：Prometheus（每副本一份 → 抓取聚合）。
    # 没配 PROMETHEUS_URL 时留 None → /admin/metrics/* 读进程内。
    app.state.prometheus = (
        PrometheusClient(settings.prometheus_url) if settings.prometheus_url.strip() else None
    )

    # 灰度自动回滚控制器（`OP-02`）：**默认关**。
    # 多副本时指标必须来自 Prometheus（进程内指标在多副本下不可比），
    # `prometheus_url` 未配置 → 指标源返回 None → 只告警不动作。
    app.state.canary = _build_canary_controller(settings, config_store, metrics_store)

    # 并发提容（批次 1）：问答路径的进程级有界执行器；Agent 运行与工具各一个
    # 独立池（图线程等待工具 future，共用一池会嵌套死锁）
    pipeline_executor = PipelineExecutor(
        workers=settings.pipeline_workers, queue_size=settings.pipeline_queue_size,
        on_reject=lambda reason="overloaded": prom.shed_total.labels(reason=reason).inc(),
        # `C-11`：低优先级（后台）排队上限更小——后台任务不该占满交互的排队位
        low_queue_size=max(1, int(settings.pipeline_queue_size) // 4),
    )
    asm.pipeline_executor = pipeline_executor
    app.state.pipeline_executor = pipeline_executor
    # 流式断点续传的帧缓冲（`Last-Event-ID`）：无论开关都需要对象，
    # 端点按 `sse_resume_enabled` 决定要不要往里写（关着就是空壳，零成本）。
    app.state.sse_replay = SseReplayStore(ttl_s=settings.sse_replay_ttl_s)
    asm.agent_run_pool = ThreadPoolExecutor(
        max_workers=settings.agent_run_permits, thread_name_prefix="agent-run"
    )
    asm.agent_tool_pool = ThreadPoolExecutor(
        max_workers=max(8, settings.agent_run_permits * 2), thread_name_prefix="agent-tool"
    )

    # 批次 2：并发水位指标（render 时采集，不另起定时线程）
    def _update_watermarks(p) -> None:
        s = pipeline_executor.stats()
        p.inflight_requests.set(s["inflight"])
        p.queue_depth.set(s["waiting"])
        for name, st in bulkheads_stats().items():
            p.upstream_in_use.labels(upstream=name).set(st.get("in_use", 0))
            # 分母 + 占用率（`C-07`）：外部自己也能算，不必读应用日志
            p.upstream_permits.labels(upstream=name).set(st.get("max_permits", 0))
            p.upstream_latency_p95.labels(upstream=name).set(st.get("p95_ms", 0.0))
            p.upstream_circuit_open.labels(upstream=name).set(
                1 if st.get("state") == "open" else 0
            )

    prom.set_stats_provider(_update_watermarks)
