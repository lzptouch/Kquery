"""运维智能体（AIOps Agent）。

- 基于 LangGraph 的 ReAct 智能体，挂载运维工具（指标/告警/健康/故障/SLO/日志）
- LLM 已配置时使用大模型决策；未配置或不可用时降级为关键词路由助手
- 对外提供 chat() 接口，支持多轮会话（按 conversation_id 维护上下文）
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app.core.llm_client import is_llm_configured
from app.core.settings import get_settings
from app.services.ops.alert_manager import AlertManager
from app.services.ops.health_checker import HealthChecker
from app.services.ops.incident_manager import IncidentManager
from app.services.ops.log_collector import LogCollector
from app.services.ops.metrics_collector import MetricsCollector
from app.services.ops.slo_tracker import SLOTracker

logger = logging.getLogger(__name__)


# ---------- 运维工具（供智能体调用） ----------

async def tool_health() -> str:
    """巡检所有组件健康状态。返回各组件 up/down 及延迟。"""
    summary = await HealthChecker.instance().run_all_checks()
    lines = [f"健康巡检：{summary['healthy']}/{summary['total']} 正常"]
    for comp, r in summary["components"].items():
        lines.append(f"  - {comp}: {r['status']} ({r.get('latency_ms', r.get('detail', ''))})")
    return "\n".join(lines)


async def tool_active_alerts() -> str:
    """列出当前活跃告警。"""
    alerts = await AlertManager.instance().list_alerts(status="active", limit=50)
    if not alerts:
        return "当前没有活跃告警。"
    lines = [f"活跃告警 {len(alerts)} 条："]
    for a in alerts:
        lines.append(f"  - [{a['severity']}] {a['title']} @ {a['component']}")
    return "\n".join(lines)


async def tool_incidents(status: str = "") -> str:
    """列出故障（可按状态过滤：investigating/diagnosed/resolved）。"""
    incidents = await IncidentManager.instance().list_incidents(status=status or None, limit=20)
    if not incidents:
        return "当前没有故障记录。"
    lines = [f"故障 {len(incidents)} 条："]
    for i in incidents:
        lines.append(
            f"  - {i['id']} [{i['severity']}] {i['status']}: {i['symptom'][:40]}"
        )
    return "\n".join(lines)


async def tool_slo_status() -> str:
    """评估并列出所有 SLO 的错误预算状态。"""
    slos = await SLOTracker.instance().list_slos()
    if not slos:
        return "尚未定义 SLO。"
    statuses = await SLOTracker.instance().all_status()
    lines = ["SLO 状态："]
    for s in statuses:
        lines.append(
            f"  - {s.get('slo_name')}: {s.get('status')} "
            f"(错误率={s.get('error_rate')}, 剩余预算={s.get('budget_remaining_ratio')})"
        )
    return "\n".join(lines)


async def tool_recent_logs(level: str = "", limit: int = 20) -> str:
    """查询最近的日志（可按级别 ERROR/WARNING/INFO 过滤）。"""
    logs = await LogCollector.instance().query(level=level or None, limit=limit)
    if not logs:
        return "暂无日志。"
    lines = [f"最近日志 {len(logs)} 条："]
    for l in logs[:limit]:
        ts = time.strftime("%H:%M:%S", time.localtime(l["timestamp"]))
        lines.append(f"  - {ts} [{l['level']}] {l['source']}: {l['message'][:80]}")
    return "\n".join(lines)


async def tool_metrics_summary() -> str:
    """返回核心运维指标概览。"""
    m = MetricsCollector.instance()
    try:
        err = round(m.error_rate.get(), 4)
    except Exception:
        err = 0.0
    alerts = await AlertManager.instance().count_active()
    incidents = await IncidentManager.instance().stats()
    return (
        f"指标概览：错误率={err}, 活跃告警={alerts}, "
        f"故障总数={incidents.get('total')}, 未闭环={incidents.get('open_count')}"
    )


_TOOLS = [tool_health, tool_active_alerts, tool_incidents, tool_slo_status, tool_recent_logs, tool_metrics_summary]


class OpsAgent:
    """运维智能体（单例）。"""

    _instance: Optional["OpsAgent"] = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self.metrics = MetricsCollector.instance()
        self._conversations: Dict[str, List[Dict[str, str]]] = {}
        self._graph_agent = None
        if is_llm_configured() and self.settings.ops.incident_use_llm:
            try:
                self._graph_agent = self._build_agent()
            except Exception as exc:  # pragma: no cover
                logger.warning("构建 ReAct 智能体失败，使用降级助手: %s", exc)
                self._graph_agent = None

    @classmethod
    def instance(cls) -> "OpsAgent":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _build_agent(self):
        from langchain_openai import ChatOpenAI
        from langgraph.prebuilt import create_react_agent

        llm = ChatOpenAI(
            model=self.settings.llm.model,
            api_key=self.settings.llm.api_key,
            base_url=self.settings.llm.base_url or None,
            temperature=0.3,
        )
        system = (
            "你是 KQuery 平台的运维智能体(AIOps)。你可以使用工具查询系统的"
            "健康状态、告警、故障、SLO 与日志，并据此回答问题或给出处置建议。"
            "回答要简洁、专业、以中文输出。"
        )
        return create_react_agent(llm, _TOOLS, prompt=system)

    # ---------- 对话 ----------

    async def chat(self, message: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        conversation_id = conversation_id or f"conv_{uuid.uuid4().hex[:12]}"
        history = self._conversations.setdefault(conversation_id, [])

        if self._graph_agent is not None:
            try:
                reply = await self._llm_chat(message, history)
                history.append({"role": "user", "content": message})
                history.append({"role": "assistant", "content": reply})
                self.metrics.inc_llm(self.settings.llm.model, "success")
                return {
                    "conversation_id": conversation_id,
                    "reply": reply,
                    "mode": "llm_agent",
                }
            except Exception as exc:
                logger.warning("LLM 智能体失败，降级: %s", exc)
                self.metrics.inc_llm(self.settings.llm.model, "error")

        reply = await self._fallback_chat(message)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": reply})
        return {"conversation_id": conversation_id, "reply": reply, "mode": "fallback"}

    async def _llm_chat(self, message: str, history: List[Dict[str, str]]) -> str:
        msgs = []
        for h in history[-10:]:
            msgs.append((h["role"], h["content"]))
        msgs.append(("user", message))
        result = await self._graph_agent.ainvoke({"messages": msgs})
        return result["messages"][-1].content

    async def _fallback_chat(self, message: str) -> str:
        msg = message.lower()
        # 收集所有命中的意图，支持一句话同时询问多个维度
        results: List[str] = []
        if any(k in msg for k in ["健康", "health", "巡检", "组件", "依赖"]):
            results.append(await tool_health())
        if any(k in msg for k in ["告警", "alert", "报警"]):
            results.append(await tool_active_alerts())
        if any(k in msg for k in ["故障", "incident", "根因", "排查"]):
            results.append(await tool_incidents(""))
        if any(k in msg for k in ["slo", "错误预算", "可用性", "目标"]):
            results.append(await tool_slo_status())
        if any(k in msg for k in ["日志", "log", "报错"]):
            results.append(await tool_recent_logs(""))
        if any(k in msg for k in ["指标", "metrics", "概览", "概况"]):
            results.append(await tool_metrics_summary())
        if results:
            return "\n\n".join(results)
        # 默认给一个总览
        overview = [
            "我可以帮你处理以下运维事务：",
            "1) 健康巡检 2) 告警查询 3) 故障诊断 4) SLO/错误预算 5) 日志检索 6) 指标概览",
            "请告诉我你想了解的内容，例如：『系统健康状态如何？』『有哪些活跃告警？』",
        ]
        return "\n".join(overview)

    async def get_history(self, conversation_id: str) -> List[Dict[str, str]]:
        return self._conversations.get(conversation_id, [])
