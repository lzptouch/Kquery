"""成本账单。

 之后 token 用量已经能采到，但只有总量、没有"这次花了多少钱、花在哪"。
本模块把用量按 **阶段（phase）× 模型** 归集成可查询的账单：

- 单次问答：`GET /ask/{trace_id}/cost` —— 用户/运维问"这次为什么贵"
- 租户聚合：`GET /admin/costs?from=&to=&group_by=phase` —— 成本看板

设计取舍：
1. **进程内环形缓冲**（非持久化）。账单是运维视图，不是账务凭证；真要做账务
   应接供应商账单，而不是信任本地估算。保留上限避免无界增长。
2. **缺价格记 0 并标 `priced=False`**。价格表过期/新增模型时，宁可显示"未知"，
   也不要给一个错误数字被用来做预算与告警。
"""
import threading
import time
from collections import deque
from dataclasses import dataclass, field

MAX_RECORDS = 5000  # 环形缓冲上限：超出后最老的记录被挤出


@dataclass
class CostRecord:
    trace_id: str
    tenant_id: str
    phase: str          # think / answer / reflect / compress
    model: str
    tokens: int
    cost_yuan: float
    priced: bool        # 价格表是否覆盖该模型（False = 成本未知，不是"免费"）
    created_at: float = field(default_factory=time.time)


class CostLedger:
    """进程内成本账本。"""

    def __init__(self, max_records: int = MAX_RECORDS) -> None:
        self._records: deque[CostRecord] = deque(maxlen=max_records)
        self._lock = threading.Lock()

    def record(
        self,
        trace_id: str,
        tenant_id: str,
        phase: str,
        model: str,
        tokens: int,
        cost_yuan: float,
        priced: bool,
    ) -> None:
        if tokens <= 0 and cost_yuan <= 0:
            return
        with self._lock:
            self._records.append(CostRecord(
                trace_id=trace_id, tenant_id=tenant_id, phase=phase, model=model,
                tokens=int(tokens), cost_yuan=float(cost_yuan), priced=bool(priced),
            ))

    def for_trace(self, trace_id: str, tenant_id: str | None = None) -> dict:
        """单次问答账单（按阶段归集）。

        tenant_id 给定时只归集该租户的记录：成本账单含模型与金额，
        跨租户读取属于越权（对照分析文档 KQ-02）。
        """
        with self._lock:
            rows = [r for r in self._records if r.trace_id == trace_id]
        if tenant_id:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        if not rows:
            return {
                "trace_id": trace_id, "total_yuan": 0.0, "total_tokens": 0,
                "by_phase": {}, "by_model": {}, "priced": True, "found": False,
            }
        by_phase: dict[str, dict] = {}
        by_model: dict[str, dict] = {}
        for r in rows:
            p = by_phase.setdefault(r.phase, {"tokens": 0, "cost_yuan": 0.0})
            p["tokens"] += r.tokens
            p["cost_yuan"] = round(p["cost_yuan"] + r.cost_yuan, 6)
            m = by_model.setdefault(r.model or "unknown", {"tokens": 0, "cost_yuan": 0.0})
            m["tokens"] += r.tokens
            m["cost_yuan"] = round(m["cost_yuan"] + r.cost_yuan, 6)
        return {
            "trace_id": trace_id,
            "total_yuan": round(sum(r.cost_yuan for r in rows), 6),
            "total_tokens": sum(r.tokens for r in rows),
            "by_phase": by_phase,
            "by_model": by_model,
            # 任一条未定价 → 总额不可信，明确告知调用方
            "priced": all(r.priced for r in rows),
            "found": True,
        }

    def aggregate(
        self,
        since: float | None = None,
        until: float | None = None,
        tenant_id: str | None = None,
        group_by: str = "phase",
    ) -> dict:
        """按维度聚合（成本看板）。"""
        with self._lock:
            rows = list(self._records)
        if since is not None:
            rows = [r for r in rows if r.created_at >= since]
        if until is not None:
            rows = [r for r in rows if r.created_at <= until]
        if tenant_id:
            rows = [r for r in rows if r.tenant_id == tenant_id]

        groups: dict[str, dict] = {}
        for r in rows:
            key = getattr(r, group_by, r.phase) or "unknown"
            g = groups.setdefault(key, {"tokens": 0, "cost_yuan": 0.0, "calls": 0})
            g["tokens"] += r.tokens
            g["cost_yuan"] = round(g["cost_yuan"] + r.cost_yuan, 6)
            g["calls"] += 1
        return {
            "group_by": group_by,
            "total_yuan": round(sum(r.cost_yuan for r in rows), 6),
            "total_tokens": sum(r.tokens for r in rows),
            "groups": groups,
            "priced": all(r.priced for r in rows) if rows else True,
            "records": len(rows),
        }

    def top_traces(self, limit: int = 10) -> list[dict]:
        """消耗最高的若干次问答（排查"是谁在烧钱"）。"""
        with self._lock:
            rows = list(self._records)
        agg: dict[str, dict] = {}
        for r in rows:
            a = agg.setdefault(r.trace_id, {"trace_id": r.trace_id, "cost_yuan": 0.0, "tokens": 0})
            a["cost_yuan"] = round(a["cost_yuan"] + r.cost_yuan, 6)
            a["tokens"] += r.tokens
        return sorted(agg.values(), key=lambda x: x["cost_yuan"], reverse=True)[:limit]


_LEDGER: CostLedger | None = None
_LOCK = threading.Lock()


def get_ledger() -> CostLedger:
    """进程级共享账本（与 PromMetrics 同模式：装配期可替换，测试可隔离）。"""
    global _LEDGER
    with _LOCK:
        if _LEDGER is None:
            _LEDGER = CostLedger()
    return _LEDGER


