"""OpenTelemetry 接入（TASK-132）。

## 在此之前

项目只有 `contextvars` 里的 trace_id + Prometheus 指标：

- **trace_id 只能靠日志 grep 串**——跨服务的调用（嵌入服务、重排服务、向量库）
  完全不可见，无法回答"这次请求慢在哪一跳"；
- 没有 span 层级，看不到"检索耗时 200ms"里各子阶段的比例。

## 接入方式

- **可选依赖**：OTel 包缺失或未配置 endpoint 时**静默降级为 NoOp**，
  绝不让"没有可观测后端"变成"服务起不来"。这个取舍和项目其它护栏一致。
- **OTLP/HTTP 导出**：`OTEL_EXPORTER_OTLP_ENDPOINT` 指向 collector（Jaeger/Tempo）。
- **采样**：默认父采样（有上游 trace 就跟随），独立请求按 `otel_sample_ratio`。

## 与 trace_id 的关系

OTel 的 `trace_id` 与项目的 16 位 hex `trace_id` **不是同一个东西**：
本项目的 trace_id 是 HTTP 层自研的请求标识（前端传给用户、用于日志检索），
OTel 的 trace_id 是 W3C 标准的 128 位标识。两者都需要，因此：
**把自研 trace_id 作为 span attribute 挂上去**，实现"用户报的号"→"Jaeger 里的链路"的跳转。
这比强行统一成一个更实际（前端已经在用 16 位短号，改了是破坏性变更）。
"""
import logging

logger = logging.getLogger(__name__)

# 全局状态：None 表示未初始化（或初始化失败），所有 span 调用退化为 no-op
_TRACER = None
_ENABLED = False


def setup_otel(settings) -> bool:
    """按配置初始化 OTel。返回是否真正启用（供启动日志与探针展示）。"""
    global _TRACER, _ENABLED

    if not bool(getattr(settings, "otel_enabled", False)):
        logger.info("OTel 未启用（otel_enabled=false），span 调用为空操作")
        return False

    endpoint = str(getattr(settings, "otel_exporter_endpoint", "") or "").strip()
    if not endpoint:
        # 显式启用了但没给 endpoint：降级并**明确告知**，避免"以为在上报其实没有"
        logger.warning("OTel 已启用但未配置 otel_exporter_endpoint，降级为 NoOp")
        return False
    # endpoint 必须做**显式校验**：OTLPSpanExporter 接受任意字符串，只在后台导出时
    # 静默失败——那会变成"接口显示已启用、实际一条都导不出去"的最坏情况
    # （比明确降级更难排查）。这里宁可拒绝，也不要假装成功。
    if not endpoint.startswith(("http://", "https://")):
        logger.warning("OTel endpoint 格式非法（需 http(s):// 前缀）: %r，降级为 NoOp", endpoint)
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        logger.warning("OTel 依赖缺失（%s），降级为 NoOp", exc)
        return False

    try:
        ratio = float(getattr(settings, "otel_sample_ratio", 1.0) or 1.0)
        resource = Resource.create({
            "service.name": str(getattr(settings, "otel_service_name", "kquery") or "kquery"),
            "service.version": _version(),
        })
        provider = TracerProvider(
            resource=resource,
            # ParentBased：上游已采样则跟随（保证分布式链路完整），
            # 无上游时按本地比例采样（控成本）
            sampler=ParentBased(TraceIdRatioBased(max(0.0, min(1.0, ratio)))),
        )
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _TRACER = trace.get_tracer("app")
        _ENABLED = True
        logger.info("OTel 已启用：endpoint=%s sample_ratio=%.2f", endpoint, ratio)
        return True
    except Exception as exc:  # noqa: BLE001 - 观测设施故障不得阻断启动
        logger.warning("OTel 初始化失败（降级为 NoOp）: %s", exc)
        return False


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("kquery-backend")
    except Exception:  # noqa: BLE001
        return "unknown"


def enabled() -> bool:
    return _ENABLED


def get_tracer():
    return _TRACER


class _NoOpSpan:
    """空 span：未启用 OTel 时替代真实 span，保证调用方代码无需分支。"""

    def set_attribute(self, *args, **kwargs) -> None:  # noqa: D102
        pass

    def record_exception(self, *args, **kwargs) -> None:  # noqa: D102
        pass

    def set_status(self, *args, **kwargs) -> None:  # noqa: D102
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def span(name: str, **attributes):
    """创建 span 的上下文管理器。

    未启用时返回 no-op 对象——**调用方不需要写 `if otel_enabled` 分支**，
    这是刻意的：让埋点代码保持"无条件可读"，否则半年后没人愿意加埋点。

    自动附带项目自研 trace_id，实现"用户报的短号"→"Jaeger 链路"的跳转。
    """
    if not _ENABLED or _TRACER is None:
        return _NoOpSpan()
    try:
        from app.core.tracing import get_trace_id

        attributes.setdefault("kquery.trace_id", get_trace_id())
    except Exception:  # noqa: BLE001
        pass
    return _TRACER.start_as_current_span(name, attributes=attributes)


def shutdown_otel(timeout_ms: int = 5000) -> None:
    """停机时 flush 未上报的 span（BatchSpanProcessor 会缓冲）。"""
    if not _ENABLED:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            provider.shutdown()
            logger.info("OTel span 已 flush 并关闭")
    except Exception as exc:  # noqa: BLE001
        logger.debug("OTel 关闭失败（忽略）: %s", exc)
