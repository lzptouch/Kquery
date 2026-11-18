"""Prometheus 查询 API 客户端（多副本下的指标真相源）。

只用标准库（urllib）：Prometheus 的查询 API 就是 HTTP + JSON，
引入 httpx 只是为了少写 20 行——不值得多一个依赖。

**为什么不用 Prometheus 自己的 client 库**：那会引入版本耦合，
而我们只需要两个接口（instant query / range query）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from app.core.logging_setup import get_logger

logger = get_logger(__name__)


class PrometheusQueryError(RuntimeError):
    """Prometheus 不可达或返回了无法解析的内容。"""


class PrometheusClient:
    """极简 Prometheus 查询客户端（只读）。"""

    def __init__(self, base_url: str, timeout_s: float = 3.0) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = timeout_s

    def _get(self, path: str, params: dict) -> list:
        url = f"{self._base}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise PrometheusQueryError(f"Prometheus HTTP {exc.code}") from exc
        except Exception as exc:  # noqa: BLE001 - 降级继续（异常已在本处被隔离，不影响对外语义）
            raise PrometheusQueryError(str(exc)) from exc
        if payload.get("status") != "success":
            raise PrometheusQueryError(str(payload.get("error") or "查询失败"))
        return payload.get("data", {}).get("result", []) or []

    def instant(self, query: str, at: str | None = None) -> list:
        """即时查询（instant query）：返回样本列表。"""
        params = {"query": query}
        if at:
            params["time"] = at
        return self._get("/api/v1/query", params)

    def range_query(
        self, query: str, start: float, end: float, step: float = 60.0
    ) -> list:
        """区间查询（range query）：返回时间序列。"""
        return self._get(
            "/api/v1/query_range",
            {
                "query": query,
                "start": f"{start:.3f}",
                "end": f"{end:.3f}",
                "step": f"{step:.3f}",
            },
        )

    # ---- 常用派生指标 ----

    def counter_total(self, metric: str, label_filter: str = "") -> float:
        """计数器总量（rate 后求和）。"""
        query = f"sum(rate({metric}{label_filter}[5m]))"
        rows = self.instant(query)
        if not rows:
            return 0.0
        value = rows[0].get("value") or [None, "0"]
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return 0.0

    def quantile(self, metric: str, q: float = 0.95, label_filter: str = "") -> float:
        """分位数（如 P95）。"""
        query = (
            f"histogram_quantile({q}, "
            f"sum(rate({metric}_bucket{label_filter}[5m])) by (le))"
        )
        rows = self.instant(query)
        if not rows:
            return 0.0
        value = rows[0].get("value") or [None, "0"]
        try:
            return float(value[1])
        except (TypeError, ValueError):
            return 0.0
