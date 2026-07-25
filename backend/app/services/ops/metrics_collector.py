"""运维指标采集能力。

提供基于 prometheus_client 的应用指标采集与暴露：
- 请求计数 / 延迟直方图
- 错误率 Gauge
- 活跃告警数 / 故障数 / LLM 调用数
- 各组件健康状态 Gauge
- SLO 错误预算剩余比例 Gauge

独立使用 CollectorRegistry，避免与 prometheus_fastapi_instrumentator
的默认注册表冲突。对外暴露 `export()` 生成 Prometheus 文本格式。
"""

from __future__ import annotations

import logging
from typing import Optional

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from app.core.settings import get_settings

logger = logging.getLogger(__name__)


class MetricsCollector:
    """应用指标采集器（单例）。"""

    _instance: Optional["MetricsCollector"] = None

    def __init__(self) -> None:
        self.settings = get_settings()
        ns = self.settings.ops.metrics_namespace
        self.registry = CollectorRegistry()
        self._custom_gauges: dict = {}

        self.request_count = Counter(
            f"{ns}_requests_total",
            "总请求数",
            ["endpoint", "method", "status"],
            registry=self.registry,
        )
        self.request_duration = Histogram(
            f"{ns}_request_duration_seconds",
            "请求处理耗时(秒)",
            ["endpoint"],
            registry=self.registry,
        )
        self.error_rate = Gauge(
            f"{ns}_error_rate",
            "当前错误率(0-1)",
            registry=self.registry,
        )
        self.active_alerts = Gauge(
            f"{ns}_active_alerts",
            "当前活跃告警数",
            registry=self.registry,
        )
        self.service_health = Gauge(
            f"{ns}_service_health",
            "组件健康状态(1=正常,0=异常)",
            ["component"],
            registry=self.registry,
        )
        self.slo_budget = Gauge(
            f"{ns}_slo_budget_remaining",
            "SLO 错误预算剩余比例(0-1)",
            ["slo"],
            registry=self.registry,
        )
        self.llm_calls = Counter(
            f"{ns}_llm_calls_total",
            "LLM 调用总数",
            ["model", "status"],
            registry=self.registry,
        )
        self.incidents_total = Counter(
            f"{ns}_incidents_total",
            "故障总数",
            ["severity"],
            registry=self.registry,
        )

    @classmethod
    def instance(cls) -> "MetricsCollector":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- 采集方法 ----------

    def record_request(self, endpoint: str, method: str, status: int, duration: float) -> None:
        if not self.settings.ops.metrics_enabled:
            return
        try:
            self.request_count.labels(endpoint=endpoint, method=method, status=str(status)).inc()
            self.request_duration.labels(endpoint=endpoint).observe(duration)
        except Exception as exc:  # pragma: no cover
            logger.warning("record_request 失败: %s", exc)

    def set_error_rate(self, value: float) -> None:
        try:
            self.error_rate.set(max(0.0, min(1.0, float(value))))
        except Exception as exc:  # pragma: no cover
            logger.warning("set_error_rate 失败: %s", exc)

    def set_active_alerts(self, n: int) -> None:
        try:
            self.active_alerts.set(int(n))
        except Exception:  # pragma: no cover
            pass

    def set_service_health(self, component: str, healthy: bool) -> None:
        try:
            self.service_health.labels(component=component).set(1 if healthy else 0)
        except Exception:  # pragma: no cover
            pass

    def set_slo_budget(self, slo: str, ratio: float) -> None:
        try:
            self.slo_budget.labels(slo=slo).set(max(0.0, min(1.0, float(ratio))))
        except Exception:  # pragma: no cover
            pass

    def inc_llm(self, model: str, status: str = "success") -> None:
        try:
            self.llm_calls.labels(model=model or "unknown", status=status).inc()
        except Exception:  # pragma: no cover
            pass

    def inc_incident(self, severity: str) -> None:
        try:
            self.incidents_total.labels(severity=severity or "unknown").inc()
        except Exception:  # pragma: no cover
            pass

    def set_custom(self, name: str, value: float) -> None:
        """设置/更新一个自定义指标（懒创建 Gauge）。"""
        safe = "".join(c if c.isalnum() else "_" for c in name)
        safe = f"{self.settings.ops.metrics_namespace}_{safe}"
        try:
            if safe not in self._custom_gauges:
                self._custom_gauges[safe] = Gauge(
                    safe, f"custom metric {name}", registry=self.registry
                )
            self._custom_gauges[safe].set(float(value))
        except Exception as exc:  # pragma: no cover
            logger.warning("set_custom 失败: %s", exc)

    # ---------- 导出 ----------

    def export(self) -> bytes:
        return generate_latest(self.registry)

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE_LATEST
