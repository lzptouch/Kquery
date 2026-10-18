"""管理与观测端点（阶段 2 前端规范 §3 / §4 / §6）。

- /admin/metrics/*：监控大盘（首屏指标 / 趋势 / 高频查询榜）；
- /admin/retrieval-config：检索参数读写（PUT 延迟生效，返回生效时刻）；
- /knowledge/stats 与 /knowledge/{doc_id}/probe 在 knowledge 路由中。

注意：指标是服务侧运营数据，不进 /ask 用户响应。
"""
import logging

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.core.envelope import Envelope, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.dependencies import require_role
from app.domain.schemas import RetrievalConfig

logger = logging.getLogger(__name__)

# 管理面端点：整 router 挂 admin 门禁（认证 + 角色校验，缺一不可）
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@router.get("/metrics/summary", response_model=Envelope)
def metrics_summary(request: Request, range_s: int = Query(default=3600, ge=60, le=86400)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    data = request.app.state.metrics_store.summary(range_s)
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


@router.get("/metrics/timeseries", response_model=Envelope)
def metrics_timeseries(
    request: Request,
    metric: str = Query(default="requests"),
    range_s: int = Query(default=3600, ge=60, le=86400),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    data = request.app.state.metrics_store.timeseries(metric, range_s)
    return ok_envelope({"metric": metric, "range_s": range_s, "points": data}, trace_id, elapsed_ms_since(started))


@router.get("/metrics/top-queries", response_model=Envelope)
def metrics_top_queries(request: Request, limit: int = Query(default=10, ge=1, le=100)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(request.app.state.metrics_store.top_queries(limit), trace_id, elapsed_ms_since(started))


@router.get("/metrics/agent", response_model=Envelope)
def metrics_agent(request: Request) -> Envelope:
    """Agent 执行指标（阶段 6 §4.1）：平均迭代/熔断率/工具分布/异常列表。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(request.app.state.metrics_store.agent_summary(), trace_id, elapsed_ms_since(started))


@router.get("/metrics/quality", response_model=Envelope)
def metrics_quality(request: Request) -> Envelope:
    """质量指标（阶段 5 前端规范 §4.1）：Faithfulness / 拒答率 / 平均重生成 / CRAG 分支占比。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    data = request.app.state.metrics_store.quality_summary()
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


@router.get("/retrieval-config", response_model=Envelope)
def get_retrieval_config(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(request.app.state.config_store.view(), trace_id, elapsed_ms_since(started))


class RetrievalConfigUpdateResult(BaseModel):
    effective_at: float
    message: str


@router.put("/retrieval-config", response_model=Envelope)
def put_retrieval_config(config: RetrievalConfig, request: Request) -> Envelope:
    """保存检索参数：不立即生效，返回生效时刻供前端提示。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    effective_at = request.app.state.config_store.update(config)
    logger.info("检索配置已提交 trace_id=%s effective_at=%s", trace_id, effective_at)
    return ok_envelope(
        RetrievalConfigUpdateResult(
            effective_at=effective_at,
            message=f"配置将在 {time_fmt(effective_at)} 生效",
        ).model_dump(),
        trace_id,
        elapsed_ms_since(started),
    )


@router.post("/retrieval-config/reset", response_model=Envelope)
def reset_retrieval_config(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    effective_at = request.app.state.config_store.reset()
    return ok_envelope(
        RetrievalConfigUpdateResult(
            effective_at=effective_at,
            message=f"已重置为默认，将在 {time_fmt(effective_at)} 生效",
        ).model_dump(),
        trace_id,
        elapsed_ms_since(started),
    )


@router.get("/costs", response_model=Envelope)
def cost_dashboard(
    request: Request,
    from_ts: float = Query(default=0.0, alias="from"),
    to_ts: float = Query(default=0.0, alias="to"),
    group_by: str = Query(default="phase", pattern="^(phase|model|tenant_id)$"),
    tenant_id: str = Query(default=""),
) -> Envelope:
    """成本看板（TASK-134）。按 phase/model/tenant 聚合，并可看消耗最高的问答。

    原先寄居在 `api/reports.py`，与多智能体报告无关，报告中心下线时随迁到此处。
    本 router 已带 `prefix="/admin"` 与整 router 的 admin 门禁，故不再重复声明。
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    from app.services.agent.cost import get_ledger

    ledger = get_ledger()
    data = ledger.aggregate(
        since=from_ts or None,
        until=to_ts or None,
        tenant_id=tenant_id or None,
        group_by=group_by,
    )
    data["top_traces"] = ledger.top_traces(limit=10)
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


def time_fmt(epoch: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")
