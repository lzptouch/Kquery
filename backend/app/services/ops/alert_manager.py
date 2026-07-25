"""运维告警能力。

提供告警规则管理与告警生命周期管理：
- 规则：基于指标阈值（>, <, >=, <=, ==）触发
- 告警：创建 / 查询 / 解决 / 统计
- 规则评估：根据当前指标快照自动触发告警（带冷却时间）
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app.core.settings import get_settings
from app.services.ops._store import StateStore
from app.services.ops.metrics_collector import MetricsCollector

logger = logging.getLogger(__name__)

VALID_OPERATORS = {">", "<", ">=", "<=", "==", "!="}


class AlertManager:
    """告警管理器（单例）。"""

    _instance: Optional["AlertManager"] = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self.store = StateStore(namespace="kquery:ops:alerts")
        self.metrics = MetricsCollector.instance()

    @classmethod
    def instance(cls) -> "AlertManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- 规则管理 ----------

    async def create_rule(
        self,
        name: str,
        metric: str,
        operator: str,
        threshold: float,
        severity: str = "warning",
        description: str = "",
        enabled: bool = True,
        cooldown_seconds: Optional[int] = None,
        component: str = "system",
    ) -> Dict[str, Any]:
        if operator not in VALID_OPERATORS:
            raise ValueError(f"不支持的运算符: {operator}，支持 {sorted(VALID_OPERATORS)}")
        rule = {
            "id": f"rule_{uuid.uuid4().hex[:12]}",
            "name": name,
            "metric": metric,
            "operator": operator,
            "threshold": float(threshold),
            "severity": severity,
            "description": description,
            "enabled": enabled,
            "component": component,
            "cooldown_seconds": cooldown_seconds or self.settings.ops.alert_default_cooldown_seconds,
            "last_triggered": None,
            "created_at": time.time(),
        }
        await self.store.set_json(f"rule:{rule['id']}", rule)
        await self._add_rule_index(rule["id"])
        return rule

    async def list_rules(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        # 规则数量有限，直接在内存索引中遍历
        keys = await self._rule_keys()
        rules: List[Dict[str, Any]] = []
        for k in keys:
            r = await self.store.get_json(k)
            if r and (not enabled_only or r.get("enabled")):
                rules.append(r)
        rules.sort(key=lambda x: x.get("created_at", 0))
        return rules

    async def get_rule(self, rule_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.get_json(f"rule:{rule_id}")

    async def delete_rule(self, rule_id: str) -> bool:
        rule = await self.get_rule(rule_id)
        if not rule:
            return False
        await self.store.delete(f"rule:{rule_id}")
        idx = await self.store.get_json("_rule_index", default=[])
        if rule_id in idx:
            idx.remove(rule_id)
            await self.store.set_json("_rule_index", idx)
        return True

    async def _rule_keys(self) -> List[str]:
        idx = await self.store.get_json("_rule_index", default=[])
        return [f"rule:{rid}" for rid in idx]

    async def _add_rule_index(self, rule_id: str) -> None:
        idx = await self.store.get_json("_rule_index", default=[])
        if rule_id not in idx:
            idx.append(rule_id)
            await self.store.set_json("_rule_index", idx)

    # ---------- 告警生命周期 ----------

    async def create_alert(
        self,
        title: str,
        severity: str = "warning",
        message: str = "",
        component: str = "system",
        rule_id: Optional[str] = None,
        metric_value: Optional[float] = None,
        annotations: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        alert = {
            "id": f"alert_{uuid.uuid4().hex[:12]}",
            "title": title,
            "severity": severity,
            "status": "active",
            "message": message,
            "component": component,
            "rule_id": rule_id,
            "metric_value": metric_value,
            "annotations": annotations or {},
            "created_at": time.time(),
            "resolved_at": None,
        }
        await self.store.push_list("active", alert)
        await self.store.set_json(f"alert:{alert['id']}", alert)
        self.metrics.set_active_alerts(await self.count_active())
        return alert

    async def list_alerts(
        self,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        alerts = await self.store.get_list("active", 0, limit - 1)
        # 已解决的告警单独存储
        resolved = await self.store.get_list("resolved", 0, limit - 1)
        all_alerts = alerts + resolved
        if status:
            all_alerts = [a for a in all_alerts if a.get("status") == status]
        if severity:
            all_alerts = [a for a in all_alerts if a.get("severity") == severity]
        all_alerts.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return all_alerts[:limit]

    async def get_alert(self, alert_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.get_json(f"alert:{alert_id}")

    async def resolve_alert(self, alert_id: str, note: str = "") -> Optional[Dict[str, Any]]:
        alert = await self.get_alert(alert_id)
        if not alert:
            return None
        if alert.get("status") == "resolved":
            return alert
        alert["status"] = "resolved"
        alert["resolved_at"] = time.time()
        if note:
            alert.setdefault("annotations", {})["resolution"] = note
        # 从活跃列表移除
        active = await self.store.get_list("active")
        active = [a for a in active if a.get("id") != alert_id]
        # 重写活跃列表（先清空再写回）
        await self._rewrite_list("active", active)
        await self.store.push_list("resolved", alert)
        await self.store.set_json(f"alert:{alert_id}", alert)
        self.metrics.set_active_alerts(await self.count_active())
        return alert

    async def count_active(self) -> int:
        return await self.store.list_len("active")

    async def stats(self) -> Dict[str, Any]:
        alerts = await self.list_alerts(limit=1000)
        by_status: Dict[str, int] = {}
        by_severity: Dict[str, int] = {}
        for a in alerts:
            by_status[a.get("status", "unknown")] = by_status.get(a.get("status", "unknown"), 0) + 1
            by_severity[a.get("severity", "unknown")] = by_severity.get(a.get("severity", "unknown"), 0) + 1
        return {
            "total": len(alerts),
            "by_status": by_status,
            "by_severity": by_severity,
            "active_count": by_status.get("active", 0),
        }

    # ---------- 规则评估 ----------

    async def evaluate_rules(self, metrics_snapshot: Dict[str, float]) -> List[Dict[str, Any]]:
        """根据指标快照评估所有启用的规则，触发满足条件的告警。"""
        triggered: List[Dict[str, Any]] = []
        now = time.time()
        for rule in await self.list_rules(enabled_only=True):
            metric = rule.get("metric")
            if metric not in metrics_snapshot:
                continue
            value = float(metrics_snapshot[metric])
            if not self._match(rule["operator"], value, rule["threshold"]):
                continue
            # 冷却时间检查
            last = rule.get("last_triggered")
            if last and (now - last) < rule.get("cooldown_seconds", 0):
                continue
            rule["last_triggered"] = now
            await self.store.set_json(f"rule:{rule['id']}", rule)
            alert = await self.create_alert(
                title=f"[{rule['name']}] {metric} {rule['operator']} {rule['threshold']}",
                severity=rule.get("severity", "warning"),
                message=rule.get("description", "") or f"{metric}={value} 触发规则 {rule['name']}",
                component=rule.get("component", "system"),
                rule_id=rule["id"],
                metric_value=value,
                annotations={"metric": metric, "operator": rule["operator"], "threshold": rule["threshold"]},
            )
            triggered.append(alert)
        return triggered

    @staticmethod
    def _match(operator: str, value: float, threshold: float) -> bool:
        if operator == ">":
            return value > threshold
        if operator == "<":
            return value < threshold
        if operator == ">=":
            return value >= threshold
        if operator == "<=":
            return value <= threshold
        if operator == "==":
            return value == threshold
        if operator == "!=":
            return value != threshold
        return False

    async def _rewrite_list(self, key: str, items: List[Dict[str, Any]]) -> None:
        # 通过先删除再依次 push 的方式重写列表（保持最新在前）
        await self.store.delete(key)
        for it in reversed(items):
            await self.store.push_list(key, it, max_len=10000)
