"""链路追踪配置。

提供基于OpenTelemetry的分布式链路追踪，支持Jaeger导出。
归属于跨层通用可观测性模块。

使用示例:
    from app.core.tracing import configure_tracing, get_tracer

    configure_tracing("kquery-pro")
    tracer = get_tracer()
    
    with tracer.start_as_current_span("process_document") as span:
        span.set_attribute("file_path", file_path)
        result = await process(file_path)
"""

from __future__ import annotations

import logging
from typing import Optional

from app.core.logger import get_logger
from app.core.settings import get_settings

logger = get_logger(__name__)

# 全局tracer实例
_tracer = None
_tracer_provider = None


def configure_tracing(
    service_name: str = "kquery-pro",
    jaeger_host: Optional[str] = None,
    jaeger_port: int = 6831,
    enabled: bool = True,
):
    """配置OpenTelemetry链路追踪。

    支持Jaeger导出，默认从settings读取配置。

    Args:
        service_name: 服务名称。
        jaeger_host: Jaeger Agent主机地址。
        jaeger_port: Jaeger Agent端口。
        enabled: 是否启用追踪。
    """
    global _tracer, _tracer_provider

    if not enabled:
        logger.info("链路追踪已禁用")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource

        settings = get_settings()
        actual_jaeger_host = jaeger_host or getattr(
            settings, "jaeger_host", None
        )

        if actual_jaeger_host:
            from opentelemetry.exporter.jaeger.thrift import JaegerExporter

            resource = Resource.create({
                "service.name": service_name,
                "service.version": "3.0.0",
            })

            _tracer_provider = TracerProvider(resource=resource)

            jaeger_exporter = JaegerExporter(
                agent_host_name=actual_jaeger_host,
                agent_port=jaeger_port,
            )

            _tracer_provider.add_span_processor(
                BatchSpanProcessor(jaeger_exporter)
            )

            trace.set_tracer_provider(_tracer_provider)
            _tracer = trace.get_tracer(service_name)

            logger.info(
                f"链路追踪已配置: service={service_name}, "
                f"jaeger={actual_jaeger_host}:{jaeger_port}"
            )
        else:
            logger.info("Jaeger未配置，链路追踪跳过")
            _tracer = trace.get_tracer(service_name)

    except ImportError as e:
        logger.warning(
            f"OpenTelemetry依赖未安装，链路追踪不可用: {e}"
        )
        _tracer = None
    except Exception as e:
        logger.error(f"链路追踪配置失败: {e}")
        _tracer = None


def get_tracer():
    """获取全局tracer实例。

    Returns:
        OpenTelemetry tracer实例，未配置时返回None。
    """
    return _tracer


def trace_span(name: str, attributes: Optional[dict] = None):
    """装饰器：为异步函数添加链路追踪span。

    Args:
        name: span名称。
        attributes: 初始属性。

    Usage:
        @trace_span("process_document")
        async def process(file_path: str):
            ...
    """
    from functools import wraps

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            tracer = get_tracer()
            if tracer is None:
                return await func(*args, **kwargs)

            with tracer.start_as_current_span(name) as span:
                if attributes:
                    for k, v in attributes.items():
                        span.set_attribute(k, v)
                try:
                    result = await func(*args, **kwargs)
                    try:
                        from opentelemetry import trace as otel_trace
                        span.set_status(otel_trace.StatusCode.OK)
                    except ImportError:
                        pass
                    return result
                except Exception as e:
                    try:
                        span.record_exception(e)
                        from opentelemetry import trace as otel_trace
                        span.set_status(otel_trace.StatusCode.ERROR)
                    except ImportError:
                        pass
                    raise

        return wrapper

    return decorator
