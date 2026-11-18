"""灰度自动回滚（`OP-02`）：灰度期间指标劣化 → 自动退灰度。

## 问题

`services/config_store.py` 的灰度覆盖层只有**人工开关**——灰度是**单向门**：
指标掉得再难看，也得有人手动去关。配置层灰度本身做得不错
（作用域、稳定哈希分桶、实验标签不进指标 label），缺的只是**闭环**。

## 三条纪律

1. **无基线 → 只告警不动作**：不能拿「无数据」当「劣化」——
   否则一次指标采集故障就会把正在跑的灰度全撤掉。
2. **样本下限**：样本太少时指标噪声大，不判劣化。
3. **回滚动作是 `clear_scoped`（整层移除覆盖）**：幂等且语义干净；
   「设为关闭」需要重新传入完整配置对象，而回滚时手里未必有当初那份配置。

## 多副本的坑

**进程内指标在多副本下不可比**（每个副本只看见自己那份）——
用单副本指标判多副本灰度会得出错误结论。因此：

- 单副本 → 读进程内指标聚合；
- 多副本 → 包装 `services/metrics_query.py` 的 Prometheus 查询；
  **`prometheus_url` 未配置时返回 None（不猜数）**。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: 比较方向
DIR_LT = "lt"   # 实验组指标 < 基线 → 劣化（如延迟）
DIR_GT = "gt"   # 实验组指标 > 基线 → 劣化（如失败数、错误率）

#: 判定结果
ACTION_ROLLBACK = "rollback"
ACTION_ALERT = "alert"        # 只告警（无基线 / 样本不足）
ACTION_OK = "ok"              # 未劣化
ACTION_NO_DATA = "no_data"    # 无数据（指标源返回 None）


@dataclass(frozen=True)
class CanaryRule:
    """一条灰度劣化判定规则。

    - `metric`：指标名（如 `error_rate`）；
    - `direction`：`lt` / `gt`（哪个方向算劣化）；
    - `threshold`：劣化阈值（绝对值，或相对基线的比例）；
    - `relative`：是否**相对基线**（`threshold` 解释为「相对劣化比例」，如 0.1 = 劣化 10%）；
    - `min_samples`：最小样本数——低于它**不判劣化**；
    - `observe_minutes`：观察窗（持续多少分钟都劣化才动手，避免毛刺）。
    """

    metric: str
    direction: str = DIR_GT
    threshold: float = 0.0
    relative: bool = False
    min_samples: int = 30
    observe_minutes: int = 5


@dataclass
class CanaryVerdict:
    """一次判定的结果。"""

    action: str = ACTION_OK
    reason: str = ""
    scope: str = ""
    rule: str = ""
    experiment_value: float | None = None
    baseline_value: float | None = None
    samples: int = 0
    details: dict = field(default_factory=dict)

    @property
    def should_rollback(self) -> bool:
        return self.action == ACTION_ROLLBACK

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "scope": self.scope,
            "rule": self.rule,
            "experiment_value": self.experiment_value,
            "baseline_value": self.baseline_value,
            "samples": self.samples,
            "details": self.details,
        }


class MetricSource:
    """指标来源协议（单副本 / 多副本各一个实现）。"""

    def value(self, metric: str, scope: str) -> tuple[float | None, int]:
        """返回 (指标值, 样本数)；**无数据时值为 None**（不返回 0 冒充）。"""
        raise NotImplementedError


class InProcessMetricSource(MetricSource):
    """进程内指标来源（**单副本**用）。

    多副本下**不可用**：每个副本只看见自己那份聚合，
    拿它判灰度会得出错误结论——用 `MultiReplicaMetricSource` 代替。
    """

    def __init__(self, store, tenant_of: dict | None = None) -> None:
        self._store = store
        self._tenant_of = tenant_of or {}

    def value(self, metric: str, scope: str) -> tuple[float | None, int]:
        if self._store is None:
            return None, 0
        getter = getattr(self._store, metric, None)
        if getter is None:
            return None, 0
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - 指标读不到就是「无数据」
            return None, 0
        if isinstance(value, dict):
            samples = int(value.get("total_requests") or 0)
            number = value.get("error_rate")
        else:
            samples = 0
            number = value
        if number is None:
            return None, samples
        return float(number), samples


class MultiReplicaMetricSource(MetricSource):
    """多副本指标来源（包装 `services/metrics_query.py`）。

    `prometheus_url` **未配置时返回 None**——不猜数、不用进程内指标冒充。
    """

    def __init__(self, prometheus_url: str = "", timeout_s: float = 3.0) -> None:
        self._url = (prometheus_url or "").strip()
        self._client = None
        if self._url:
            # 按需载入：Prometheus 客户端只在真配了多副本指标源时才需要
            from app.services.metrics_query import PrometheusClient

            self._client = PrometheusClient(self._url, timeout_s=timeout_s)

    @property
    def configured(self) -> bool:
        return self._client is not None

    def value(self, metric: str, scope: str) -> tuple[float | None, int]:
        if self._client is None:
            return None, 0
        try:
            label = f'scope="{scope}"' if scope else ""
            value = self._client.counter_total(metric, label_filter=label)
        except Exception:  # noqa: BLE001 - 查询失败就是「无数据」
            return None, 0
        return float(value), 0


def evaluate_rule(
    rule: CanaryRule,
    experiment_value: float | None,
    baseline_value: float | None,
    samples: int = 0,
) -> CanaryVerdict:
    """按规则判定一次（**纯函数**，便于测试与复用）。"""
    verdict = CanaryVerdict(rule=rule.metric)
    verdict.experiment_value = experiment_value
    verdict.baseline_value = baseline_value
    verdict.samples = samples

    # 纪律 1：无基线 → 只告警不动作
    if baseline_value is None or experiment_value is None:
        verdict.action = ACTION_NO_DATA
        verdict.reason = "无基线或无实验组数据：只告警，不动作（不能拿「无数据」当「劣化」）"
        return verdict

    # 样本数下限
    if samples < rule.min_samples:
        verdict.action = ACTION_ALERT
        verdict.reason = f"样本数 {samples} < 下限 {rule.min_samples}：不判劣化"
        return verdict

    if rule.relative:
        if baseline_value == 0:
            verdict.action = ACTION_ALERT
            verdict.reason = "基线为 0，无法算相对劣化：只告警"
            return verdict
        delta = (experiment_value - baseline_value) / abs(baseline_value)
        worse = delta > rule.threshold if rule.direction == DIR_GT else delta < -rule.threshold
        verdict.details = {"relative_delta": round(delta, 6)}
    else:
        delta = experiment_value - baseline_value
        worse = (
            experiment_value > rule.threshold
            if rule.direction == DIR_GT
            else experiment_value < rule.threshold
        )
        verdict.details = {"delta": round(delta, 6)}

    if worse:
        verdict.action = ACTION_ROLLBACK
        verdict.reason = (
            f"指标 {rule.metric} 劣化：实验组 {experiment_value} vs 基线 {baseline_value}"
            f"（{rule.direction} {rule.threshold}{'（相对）' if rule.relative else ''}）"
        )
    else:
        verdict.action = ACTION_OK
        verdict.reason = f"指标 {rule.metric} 未劣化（实验组 {experiment_value}）"
    return verdict


class CanaryController:
    """灰度控制器：规则集 + 指标来源 + 配置存储 + 基线提供器。"""

    def __init__(
        self,
        config_store=None,
        metric_source: MetricSource | None = None,
        baseline_provider=None,
        rules: list[CanaryRule] | None = None,
        enabled: bool = False,
        on_rollback=None,
    ) -> None:
        self._config_store = config_store
        self._metrics = metric_source
        self._baseline = baseline_provider
        self._rules = list(rules or [])
        # **保守默认**：新增开关默认关（灰度是人工开的，自动回滚也该由人工开）
        self._enabled = bool(enabled)
        self._on_rollback = on_rollback

    @property
    def enabled(self) -> bool:
        return self._enabled

    def rules(self) -> list[CanaryRule]:
        return list(self._rules)

    def check_scope(self, scope: str) -> CanaryVerdict:
        """判定一个作用域（**不改动任何状态**）。"""
        if not self._enabled:
            return CanaryVerdict(action=ACTION_OK, reason="灰度自动回滚未启用", scope=scope)
        if self._metrics is None:
            return CanaryVerdict(action=ACTION_NO_DATA, reason="无指标来源", scope=scope)
        worst = CanaryVerdict(action=ACTION_OK, reason="未劣化", scope=scope)
        for rule in self._rules:
            exp_value, samples = self._metrics.value(rule.metric, scope)
            base_value = None
            if self._baseline is not None:
                try:
                    base_value = self._baseline(rule.metric, scope)
                except Exception:  # noqa: BLE001 - 基线读不到就是「无基线」
                    base_value = None
            verdict = evaluate_rule(rule, exp_value, base_value, samples)
            verdict.scope = scope
            if verdict.action == ACTION_ROLLBACK:
                return verdict
            if verdict.action != ACTION_OK:
                worst = verdict
        return worst

    def rollback(self, scope: str) -> CanaryVerdict:
        """判定 + **执行回滚**（劣化时移除覆盖层）。"""
        verdict = self.check_scope(scope)
        if verdict.action != ACTION_ROLLBACK:
            return verdict
        if self._config_store is not None:
            try:
                removed = self._config_store.clear_scoped(scope)
                verdict.details["scoped_removed"] = removed
                if removed:
                    logger.warning("灰度自动回滚：已移除作用域 %s 的覆盖层", scope)
            except Exception as exc:  # noqa: BLE001 - 回滚失败要留痕
                verdict.reason += f"；但移除覆盖层失败：{exc}"
        if self._on_rollback is not None:
            try:
                self._on_rollback(scope, verdict)
            except Exception:  # noqa: BLE001 - 回调失败不影响回滚结果
                pass
        return verdict
