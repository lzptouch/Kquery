"""运维能力 API 路由。

聚合指标 / 告警 / 日志 / 故障 / 健康 / SLO / 智能体 七大类接口，
并提供一个统一的 dashboard 概览接口供前端调用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.services.ops.alert_manager import AlertManager
from app.services.ops.health_checker import HealthChecker
from app.services.ops.incident_manager import IncidentManager
from app.services.ops.log_collector import LogCollector
from app.services.ops.metrics_collector import MetricsCollector
from app.services.ops.ops_agent import OpsAgent
from app.services.ops.slo_tracker import SLOTracker

router = APIRouter(prefix="/ops", tags=["ops"])


# ---------- 请求模型 ----------

class MetricRecord(BaseModel):
    name: str
    value: float
    kind: str = Field(default="gauge", description="gauge/counter")


class AlertCreate(BaseModel):
    title: str
    severity: str = "warning"
    message: str = ""
    component: str = "system"
    rule_id: Optional[str] = None
    metric_value: Optional[float] = None
    annotations: Optional[Dict[str, Any]] = None


class AlertRuleCreate(BaseModel):
    name: str
    metric: str
    operator: str
    threshold: float
    severity: str = "warning"
    description: str = ""
    enabled: bool = True
    cooldown_seconds: Optional[int] = None
    component: str = "system"


class RuleEvaluate(BaseModel):
    metrics: Optional[Dict[str, float]] = None


class IncidentCreate(BaseModel):
    symptom: str
    affected_services: Optional[List[str]] = None
    severity: Optional[str] = None
    use_llm: Optional[bool] = None


class IncidentResolve(BaseModel):
    resolution: str = ""


class SloCreate(BaseModel):
    name: str
    objective: Optional[float] = None
    window_days: Optional[int] = None
    metric: str = "error_rate"
    description: str = ""
    enabled: bool = True


class SloEvaluate(BaseModel):
    error_rate: Optional[float] = None


class LogCreate(BaseModel):
    source: str
    level: str = "INFO"
    message: str
    metadata: Optional[Dict[str, Any]] = None


class AgentChat(BaseModel):
    message: str
    conversation_id: Optional[str] = None


# ---------- 单例获取 ----------

def _metrics() -> MetricsCollector:
    return MetricsCollector.instance()

def _alerts() -> AlertManager:
    return AlertManager.instance()

def _logs() -> LogCollector:
    return LogCollector.instance()

def _incidents() -> IncidentManager:
    return IncidentManager.instance()

def _health() -> HealthChecker:
    return HealthChecker.instance()

def _slo() -> SLOTracker:
    return SLOTracker.instance()

def _agent() -> OpsAgent:
    return OpsAgent.instance()


# ---------- 指标 ----------

@router.get("/metrics")
async def get_metrics():
    """导出 Prometheus 格式指标。"""
    m = _metrics()
    return Response(content=m.export(), media_type=m.content_type)


@router.post("/metrics/record")
async def record_metric(body: MetricRecord):
    """记录/更新一个自定义指标（用于演示与压测注入）。"""
    m = _metrics()
    if body.kind == "counter":
        # 计数器仅支持 +1 语义时用于错误率告警等场景；此处统一按 gauge 处理
        m.set_custom(body.name, body.value)
    else:
        m.set_custom(body.name, body.value)
    return {"ok": True, "name": body.name, "value": body.value}


# ---------- 健康巡检 ----------

@router.get("/health")
async def health_overview():
    """巡检所有组件并返回概览。"""
    return await _health().run_all_checks()


@router.post("/health/check")
async def health_check_run():
    """触发一次完整健康巡检。"""
    return await _health().run_all_checks()


@router.get("/health/{component}")
async def health_component(component: str):
    """巡检单个组件。"""
    try:
        return await _health().run_check(component)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/health/{component}/history")
async def health_history(component: str, limit: int = Query(20, ge=1, le=200)):
    return await _health().get_history(component, limit)


# ---------- 告警 ----------

@router.get("/alerts")
async def list_alerts(
    status: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    return await _alerts().list_alerts(status=status, severity=severity, limit=limit)


@router.post("/alerts", status_code=201)
async def create_alert(body: AlertCreate):
    return await _alerts().create_alert(
        title=body.title,
        severity=body.severity,
        message=body.message,
        component=body.component,
        rule_id=body.rule_id,
        metric_value=body.metric_value,
        annotations=body.annotations,
    )


@router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: str, body: IncidentResolve):
    alert = await _alerts().resolve_alert(alert_id, body.resolution)
    if not alert:
        raise HTTPException(status_code=404, detail="告警不存在")
    return alert


@router.get("/alerts/stats")
async def alert_stats():
    return await _alerts().stats()


@router.get("/alert-rules")
async def list_rules():
    return await _alerts().list_rules()


@router.post("/alert-rules", status_code=201)
async def create_rule(body: AlertRuleCreate):
    try:
        return await _alerts().create_rule(
            name=body.name,
            metric=body.metric,
            operator=body.operator,
            threshold=body.threshold,
            severity=body.severity,
            description=body.description,
            enabled=body.enabled,
            cooldown_seconds=body.cooldown_seconds,
            component=body.component,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/alert-rules/{rule_id}")
async def delete_rule(rule_id: str):
    if not await _alerts().delete_rule(rule_id):
        raise HTTPException(status_code=404, detail="规则不存在")
    return {"ok": True}


@router.post("/alert-rules/evaluate")
async def evaluate_rules(body: RuleEvaluate):
    """根据指标快照评估告警规则，返回本次触发的告警。"""
    snapshot = body.metrics or {}
    # 注入当前实时错误率作为默认快照，便于演示
    try:
        snapshot.setdefault("error_rate", _metrics().error_rate.get() or 0.0)
    except Exception:
        pass
    triggered = await _alerts().evaluate_rules(snapshot)
    return {"evaluated": len(triggered), "triggered": triggered}


# ---------- 故障诊断 ----------

@router.get("/incidents")
async def list_incidents(
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
):
    return await _incidents().list_incidents(status=status, limit=limit)


@router.post("/incidents", status_code=201)
async def create_incident(body: IncidentCreate):
    return await _incidents().create_incident(
        symptom=body.symptom,
        affected_services=body.affected_services,
        severity=body.severity,
        use_llm=body.use_llm,
    )


@router.get("/incidents/stats")
async def incident_stats():
    return await _incidents().stats()


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    inc = await _incidents().get_incident(incident_id)
    if not inc:
        raise HTTPException(status_code=404, detail="故障不存在")
    return inc


@router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str, body: IncidentResolve):
    inc = await _incidents().resolve_incident(incident_id, body.resolution)
    if not inc:
        raise HTTPException(status_code=404, detail="故障不存在")
    return inc


# ---------- SLO ----------

@router.get("/slos")
async def list_slos():
    return await _slo().list_slos()


@router.post("/slos", status_code=201)
async def create_slo(body: SloCreate):
    return await _slo().define_slo(
        name=body.name,
        objective=body.objective,
        window_days=body.window_days,
        metric=body.metric,
        description=body.description,
        enabled=body.enabled,
    )


@router.delete("/slos/{slo_id}")
async def delete_slo(slo_id: str):
    if not await _slo().delete_slo(slo_id):
        raise HTTPException(status_code=404, detail="SLO 不存在")
    return {"ok": True}


@router.post("/slos/{slo_id}/evaluate")
async def evaluate_slo(slo_id: str, body: SloEvaluate):
    ev = await _slo().evaluate_slo(slo_id, current_error_rate=body.error_rate)
    if not ev:
        raise HTTPException(status_code=404, detail="SLO 不存在")
    return ev


# ---------- 日志 ----------

@router.get("/logs")
async def query_logs(
    source: Optional[str] = Query(None),
    level: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    since: Optional[float] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
):
    return await _logs().query(
        source=source, level=level, since=since, keyword=keyword, limit=limit
    )


@router.post("/logs", status_code=201)
async def collect_log(body: LogCreate):
    return await _logs().collect(
        source=body.source, level=body.level, message=body.message, metadata=body.metadata
    )


@router.get("/logs/stats")
async def log_stats():
    return await _logs().stats()


# ---------- 运维智能体 ----------

@router.post("/agent/chat")
async def agent_chat(body: AgentChat):
    return await _agent().chat(message=body.message, conversation_id=body.conversation_id)


# ---------- 统一概览（前端仪表盘） ----------

@router.get("/dashboard")
async def dashboard():
    """聚合所有运维数据，供前端仪表盘一次性拉取。"""
    m = _metrics()
    try:
        error_rate = round(m.error_rate.get() or 0.0, 4)
    except Exception:
        error_rate = 0.0
    health = await _health().get_last_summary()
    if health is None:
        health = await _health().run_all_checks()
    alerts = await _alerts().stats()
    incidents = await _incidents().stats()
    slos = await _slo().all_status()
    logs = await _logs().stats()
    return {
        "error_rate": error_rate,
        "health": health,
        "alerts": alerts,
        "incidents": incidents,
        "slos": slos,
        "logs": logs,
        "generated_at": __import__("time").time(),
    }
