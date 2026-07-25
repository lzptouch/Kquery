"""SLO 追踪与错误预算能力。

- 定义 SLO（目标可用性 / 统计窗口 / 关联指标）
- 基于当前错误率估算错误预算消耗，给出 healthy / warning / exhausted 状态
- 将剩余预算比例写入 MetricsCollector 指标，便于在 Grafana 中可视
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app.core.settings import get_settings
from app.services.ops._store import StateStore
from app.services.ops.log_collector import LogCollector
from app.services.ops.metrics_collector import MetricsCollector

logger = logging.getLogger(__name__)


class SLOTracker:
    """SLO 追踪器（单例）。"""

    _instance: Optional["SLOTracker"] = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self.store = StateStore(namespace="kquery:ops:slo")
        self.metrics = MetricsCollector.instance()
        self.logs = LogCollector.instance()

    @classmethod
    def instance(cls) -> "SLOTracker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- SLO 定义 ----------

    async def define_slo(
        self,
        name: str,
        objective: Optional[float] = None,
        window_days: Optional[int] = None,
        metric: str = "error_rate",
        description: str = "",
        enabled: bool = True,
    ) -> Dict[str, Any]:
        slo = {
            "id": f"slo_{uuid.uuid4().hex[:12]}",
            "name": name,
            "objective": objective if objective is not None else self.settings.ops.slo_default_objective,
            "window_days": window_days or self.settings.ops.slo_window_days,
            "metric": metric,
            "description": description,
            "enabled": enabled,
            "created_at": time.time(),
        }
        await self.store.set_json(f"slo:{slo['id']}", slo)
        await self._add_index(slo["id"])
        return slo

    async def list_slos(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        ids = await self.store.get_json("_slo_index", default=[])
        slos: List[Dict[str, Any]] = []
        for sid in ids:
            s = await self.store.get_json(f"slo:{sid}")
            if s and (not enabled_only or s.get("enabled")):
                slos.append(s)
        slos.sort(key=lambda x: x.get("created_at", 0))
        return slos

    async def get_slo(self, slo_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.get_json(f"slo:{slo_id}")

    async def delete_slo(self, slo_id: str) -> bool:
        s = await self.get_slo(slo_id)
        if not s:
            return False
        await self.store.delete(f"slo:{slo_id}")
        idx = await self.store.get_json("_slo_index", default=[])
        if slo_id in idx:
            idx.remove(slo_id)
            await self.store.set_json("_slo_index", idx)
        return True

    async def _add_index(self, slo_id: str) -> None:
        idx = await self.store.get_json("_slo_index", default=[])
        if slo_id not in idx:
            idx.append(slo_id)
            await self.store.set_json("_slo_index", idx)

    # ---------- 评估 ----------

    async def _current_error_rate(self) -> float:
        try:
            gauge_val = self.metrics.error_rate.get()
            if gauge_val and gauge_val > 0:
                return float(gauge_val)
        except Exception:
            pass
        # 退而求其次：基于最近日志的错误占比估算
        try:
            stats = await self.logs.stats()
            total = stats.get("total_in_window", 0)
            errors = stats.get("by_level", {}).get("ERROR", 0)
            if total > 0:
                return errors / total
        except Exception:
            pass
        return 0.0

    async def evaluate_slo(
        self, slo_id: str, current_error_rate: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        slo = await self.get_slo(slo_id)
        if not slo:
            return None
        error_rate = current_error_rate if current_error_rate is not None else await self._current_error_rate()
        error_rate = max(0.0, min(1.0, float(error_rate)))
        budget = (100.0 - float(slo["objective"])) / 100.0
        if budget <= 0:
            remaining = 1.0
            burn_rate = 0.0
            status = "healthy"
        else:
            remaining = max(0.0, (budget - error_rate) / budget)
            burn_rate = error_rate / budget
            if error_rate > budget:
                status = "exhausted"
            elif remaining <= 0.5:
                status = "warning"
            else:
                status = "healthy"

        evaluation = {
            "slo_id": slo_id,
            "slo_name": slo["name"],
            "objective": slo["objective"],
            "error_rate": round(error_rate, 6),
            "error_budget": round(budget, 6),
            "budget_remaining_ratio": round(remaining, 6),
            "burn_rate": round(burn_rate, 4),
            "status": status,
            "evaluated_at": time.time(),
        }
        await self.store.push_list(f"evals:{slo_id}", evaluation, max_len=200)
        await self.store.set_json(f"last_eval:{slo_id}", evaluation)
        self.metrics.set_slo_budget(slo["name"], remaining)
        return evaluation

    async def get_last_evaluation(self, slo_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.get_json(f"last_eval:{slo_id}")

    async def all_status(self) -> List[Dict[str, Any]]:
        slos = await self.list_slos()
        result: List[Dict[str, Any]] = []
        for s in slos:
            ev = await self.get_last_evaluation(s["id"])
            result.append(ev or {"slo_id": s["id"], "slo_name": s["name"], "status": "not_evaluated"})
        return result
