"""故障诊断与根因分析能力（基于 LangGraph 工作流）。

工作流节点：
  detect(创建) -> correlate(关联告警/日志) -> diagnose(根因诊断)
  -> recommend(处置建议) -> summarize(总结) -> human_review(人工确认)

- 当 LLM 已配置且 OPS_INCIDENT_USE_LLM=true 时，诊断与建议由大模型生成；
- 否则走规则化启发式诊断，保证在无外部依赖时仍可运行。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional, TypedDict

from app.core.llm_client import call_openai_chat, is_llm_configured
from app.core.settings import get_settings
from app.services.ops._store import StateStore
from app.services.ops.alert_manager import AlertManager
from app.services.ops.log_collector import LogCollector
from app.services.ops.metrics_collector import MetricsCollector

logger = logging.getLogger(__name__)


class IncidentState(TypedDict, total=False):
    incident_id: str
    symptom: str
    affected_services: List[str]
    severity: str
    related_alerts: List[Dict[str, Any]]
    related_logs: List[Dict[str, Any]]
    root_cause: str
    diagnosis: str
    recommendations: List[str]
    confidence: float
    status: str
    history: List[str]
    use_llm: bool


class IncidentManager:
    """故障管理器（单例），内部使用 LangGraph 工作流。"""

    _instance: Optional["IncidentManager"] = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self.store = StateStore(namespace="kquery:ops:incidents")
        self.metrics = MetricsCollector.instance()
        self.alerts = AlertManager.instance()
        self.logs = LogCollector.instance()
        self._graph = self._build_graph()

    @classmethod
    def instance(cls) -> "IncidentManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- 工作流构建 ----------

    def _build_graph(self):
        try:
            from langgraph.graph import StateGraph, END

            builder = StateGraph(IncidentState)
            builder.add_node("detect", self._node_detect)
            builder.add_node("correlate", self._node_correlate)
            builder.add_node("diagnose", self._node_diagnose)
            builder.add_node("recommend", self._node_recommend)
            builder.add_node("summarize", self._node_summarize)
            builder.add_node("human_review", self._node_human_review)
            builder.set_entry_point("detect")
            builder.add_edge("detect", "correlate")
            builder.add_edge("correlate", "diagnose")
            builder.add_edge("diagnose", "recommend")
            builder.add_edge("recommend", "summarize")
            builder.add_edge("summarize", "human_review")
            builder.add_edge("human_review", END)
            return builder.compile()
        except Exception as exc:  # pragma: no cover
            logger.warning("LangGraph 构建失败，将使用顺序执行: %s", exc)
            return None

    # ---------- 节点实现 ----------

    def _infer_severity(self, symptom: str) -> str:
        s = (symptom or "").lower()
        if any(k in s for k in ["down", "宕机", "不可用", "unavailable", "crash", "崩溃", "断连", "outage"]):
            return "critical"
        if any(k in s for k in ["slow", "缓慢", "延迟", "latency", "timeout", "超时", "抖动", "高负载"]):
            return "warning"
        return "warning"

    async def _node_detect(self, state: IncidentState) -> Dict[str, Any]:
        severity = state.get("severity") or self._infer_severity(state.get("symptom", ""))
        return {
            "incident_id": state.get("incident_id")
            or f"inc_{uuid.uuid4().hex[:12]}",
            "severity": severity,
            "status": "investigating",
            "history": [f"[{_now()}] 检测到故障症状，初始级别={severity}"],
            "confidence": 0.0,
        }

    async def _node_correlate(self, state: IncidentState) -> Dict[str, Any]:
        related_alerts: List[Dict[str, Any]] = []
        related_logs: List[Dict[str, Any]] = []
        try:
            active = await self.alerts.list_alerts(status="active", limit=50)
            services = [s.lower() for s in state.get("affected_services", [])]
            for a in active:
                if not services or a.get("component", "").lower() in services:
                    related_alerts.append(a)
            err_logs = await self.logs.query(level="ERROR", limit=50)
            for log in err_logs:
                msg = log.get("message", "").lower()
                if not services or any(s in msg for s in services):
                    related_logs.append(log)
        except Exception as exc:
            logger.warning("correlate 失败: %s", exc)
        note = (
            f"[{_now()}] 关联 {len(related_alerts)} 条活跃告警、"
            f"{len(related_logs)} 条错误日志"
        )
        return {
            "related_alerts": related_alerts[:20],
            "related_logs": related_logs[:20],
            "history": state.get("history", []) + [note],
        }

    async def _node_diagnose(self, state: IncidentState) -> Dict[str, Any]:
        use_llm = state.get("use_llm", True) and is_llm_configured()
        if use_llm:
            try:
                result = await self._llm_diagnose(state)
                return result
            except Exception as exc:
                logger.warning("LLM 诊断失败，降级规则诊断: %s", exc)
        # 规则化启发式诊断
        root_cause, diagnosis, confidence = self._rule_diagnose(state)
        return {
            "root_cause": root_cause,
            "diagnosis": diagnosis,
            "confidence": confidence,
            "history": state.get("history", [])
            + [f"[{_now()}] 完成根因诊断（规则引擎），置信度={confidence}"],
        }

    async def _node_recommend(self, state: IncidentState) -> Dict[str, Any]:
        use_llm = state.get("use_llm", True) and is_llm_configured()
        recs: List[str] = []
        if use_llm:
            try:
                recs = await self._llm_recommend(state)
            except Exception as exc:
                logger.warning("LLM 建议失败，降级模板建议: %s", exc)
        if not recs:
            recs = self._rule_recommend(state)
        return {
            "recommendations": recs,
            "history": state.get("history", [])
            + [f"[{_now()}] 生成 {len(recs)} 条处置建议"],
        }

    async def _node_summarize(self, state: IncidentState) -> Dict[str, Any]:
        summary = self._build_summary(state)
        return {
            "status": "diagnosed",
            "diagnosis": summary,
            "history": state.get("history", [])
            + [f"[{_now()}] 诊断总结完成，状态=diagnosed"],
        }

    async def _node_human_review(self, state: IncidentState) -> Dict[str, Any]:
        return {
            "history": state.get("history", [])
            + [f"[{_now()}] 待人工确认/处置（可通过 resolve 接口闭环）"],
        }

    # ---------- LLM 调用 ----------

    async def _llm_diagnose(self, state: IncidentState) -> Dict[str, Any]:
        ctx = self._build_context(state)
        prompt = (
            "你是一名资深 SRE。请根据以下故障上下文，输出 JSON："
            '{"root_cause": str, "diagnosis": str, "confidence": float(0-1)}。\n'
            f"上下文:\n{ctx}"
        )
        resp = await call_openai_chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        data = _safe_json(text)
        root_cause = data.get("root_cause", "未知")
        diagnosis = data.get("diagnosis", "")
        confidence = float(data.get("confidence", 0.5))
        return {
            "root_cause": root_cause,
            "diagnosis": diagnosis,
            "confidence": confidence,
            "history": state.get("history", [])
            + [f"[{_now()}] 完成根因诊断（LLM），置信度={confidence}"],
        }

    async def _llm_recommend(self, state: IncidentState) -> List[str]:
        ctx = self._build_context(state)
        prompt = (
            "你是一名资深 SRE。针对以下故障，给出 3-5 条可执行的处置建议，"
            "每行一条，不要编号以外的多余格式。\n"
            f"上下文:\n{ctx}"
        )
        resp = await call_openai_chat(
            [{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=600,
        )
        text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        recs = [ln.strip(" -0123456789.") for ln in text.splitlines() if ln.strip()]
        return recs[:5]

    # ---------- 规则化诊断 ----------

    def _rule_diagnose(self, state: IncidentState):
        alerts = state.get("related_alerts", [])
        logs = state.get("related_logs", [])
        if logs:
            top = logs[0]
            root_cause = f"检测到错误日志：{top.get('source')} - {top.get('message')}"
            diagnosis = (
                "基于错误日志的启发式分析：最近出现错误来自 "
                f"{top.get('source')}，建议优先排查该组件。"
            )
            confidence = 0.6
        elif alerts:
            top = alerts[0]
            root_cause = f"触发告警规则：{top.get('title')}"
            diagnosis = (
                f"存在 {len(alerts)} 条活跃告警，最高优先级为 "
                f"[{top.get('severity')}] {top.get('title')}。"
            )
            confidence = 0.55
        else:
            root_cause = "暂无足够的可观测数据（告警/日志）支撑精确定位"
            diagnosis = "建议补充指标/日志采集后重新诊断，或人工介入排查。"
            confidence = 0.3
        return root_cause, diagnosis, confidence

    def _rule_recommend(self, state: IncidentState) -> List[str]:
        sev = state.get("severity", "warning")
        recs = [
            "立即检查受影响服务的运行日志与健康巡检结果",
            "确认相关依赖（数据库/缓存/向量库）连接状态",
            "如为可用性下降，尝试扩容或重启异常实例",
        ]
        if sev == "critical":
            recs.insert(0, "已升级为严重故障，请立即通知值班并启动应急预案")
        recs.append("诊断完成后通过 resolve 接口闭环并记录处置过程")
        return recs

    # ---------- 工具 ----------

    def _build_context(self, state: IncidentState) -> str:
        parts = [
            f"故障症状: {state.get('symptom')}",
            f"影响服务: {', '.join(state.get('affected_services', []) or ['未知'])}",
            f"级别: {state.get('severity')}",
        ]
        if state.get("related_alerts"):
            parts.append("活跃告警:")
            for a in state["related_alerts"][:5]:
                parts.append(f"  - [{a.get('severity')}] {a.get('title')}")
        if state.get("related_logs"):
            parts.append("近期错误日志:")
            for l in state["related_logs"][:5]:
                parts.append(f"  - {l.get('source')}: {l.get('message')}")
        return "\n".join(parts)

    def _build_summary(self, state: IncidentState) -> str:
        return (
            f"根因: {state.get('root_cause', '未知')}\n"
            f"分析: {state.get('diagnosis', '')}\n"
            f"置信度: {state.get('confidence', 0)}\n"
            f"建议: {'; '.join(state.get('recommendations', []))}"
        )

    # ---------- 对外接口 ----------

    async def create_incident(
        self,
        symptom: str,
        affected_services: Optional[List[str]] = None,
        severity: Optional[str] = None,
        use_llm: Optional[bool] = None,
    ) -> Dict[str, Any]:
        use_llm = self.settings.ops.incident_use_llm if use_llm is None else use_llm
        if not self.settings.ops.incident_auto_diagnose:
            use_llm = False
        initial: IncidentState = {
            "symptom": symptom,
            "affected_services": affected_services or [],
            "severity": severity or "",
            "use_llm": use_llm,
        }
        if self._graph is not None:
            final = await self._graph.ainvoke(initial)
        else:
            final = await self._run_sequential(initial)
        incident = {
            "id": final.get("incident_id", f"inc_{uuid.uuid4().hex[:12]}"),
            "symptom": symptom,
            "affected_services": affected_services or [],
            "severity": final.get("severity", "warning"),
            "status": final.get("status", "diagnosed"),
            "root_cause": final.get("root_cause", ""),
            "diagnosis": final.get("diagnosis", ""),
            "recommendations": final.get("recommendations", []),
            "confidence": final.get("confidence", 0.0),
            "related_alerts": final.get("related_alerts", []),
            "related_logs": final.get("related_logs", []),
            "history": final.get("history", []),
            "created_at": time.time(),
            "resolved_at": None,
            "resolution": "",
        }
        await self.store.push_list("all", incident)
        await self.store.set_json(f"incident:{incident['id']}", incident)
        self.metrics.inc_incident(incident["severity"])
        return incident

    async def _run_sequential(self, initial: IncidentState) -> IncidentState:
        state: IncidentState = dict(initial)
        for node in [
            self._node_detect,
            self._node_correlate,
            self._node_diagnose,
            self._node_recommend,
            self._node_summarize,
            self._node_human_review,
        ]:
            update = await node(state)
            state.update(update)  # type: ignore
        return state

    async def list_incidents(
        self, status: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        incidents = await self.store.get_list("all", 0, limit - 1)
        if status:
            incidents = [i for i in incidents if i.get("status") == status]
        incidents.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return incidents[:limit]

    async def get_incident(self, incident_id: str) -> Optional[Dict[str, Any]]:
        return await self.store.get_json(f"incident:{incident_id}")

    async def resolve_incident(
        self, incident_id: str, resolution: str = ""
    ) -> Optional[Dict[str, Any]]:
        incident = await self.get_incident(incident_id)
        if not incident:
            return None
        incident["status"] = "resolved"
        incident["resolved_at"] = time.time()
        incident["resolution"] = resolution
        incident.setdefault("history", []).append(f"[{_now()}] 已闭环: {resolution or '人工确认'}")
        await self.store.set_json(f"incident:{incident_id}", incident)
        return incident

    async def stats(self) -> Dict[str, Any]:
        incidents = await self.list_incidents(limit=1000)
        by_status: Dict[str, int] = {}
        by_severity: Dict[str, int] = {}
        for i in incidents:
            by_status[i.get("status", "unknown")] = by_status.get(i.get("status", "unknown"), 0) + 1
            by_severity[i.get("severity", "unknown")] = by_severity.get(i.get("severity", "unknown"), 0) + 1
        return {
            "total": len(incidents),
            "by_status": by_status,
            "by_severity": by_severity,
            "open_count": by_status.get("diagnosed", 0) + by_status.get("investigating", 0),
        }


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


def _safe_json(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        try:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(text[start:end])
        except Exception:
            pass
    return {}
