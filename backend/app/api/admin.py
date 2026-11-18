"""管理与观测端点。

- /admin/metrics/*：监控大盘（首屏指标 / 趋势 / 高频查询榜）；
- /admin/retrieval-config：检索参数读写（PUT 延迟生效，返回生效时刻）；
- /admin/tools：工具目录与健康状态；
- /knowledge/stats 与 /knowledge/{doc_id}/probe 在 knowledge 路由中。

注意：指标是服务侧运营数据，不进 /ask 用户响应。

## 角色门禁已下线（2026-11-12）

原 router 挂 `require_role("admin")`。认证与角色体系删除后，管理面端点不再校验
角色——**服务端的访问边界由部署层承担**（反向代理 / 内网 / 网关），应用不再
自建身份体系。这是"删除权限管理"的直接后果，已登记在
`doc/analysis/08-安全与治理.md` 的已下线留档里。
"""
import time

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from app.core.envelope import ApiError, ErrCode, Envelope, ok_envelope
from app.core.tracing import elapsed_ms_since, get_trace_id, now_ms, set_trace_id
from app.dependencies import enforce_rate_limit
from app.domain.schemas import RetrievalConfig
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


def _prom_client(request: Request):
    """已配置 Prometheus 时返回查询客户端，否则 None（回落进程内）。"""
    return getattr(request.app.state, "prometheus", None)


def _summary_from_prometheus(client) -> dict | None:
    """从 Prometheus 汇总（失败返回 None → 回落进程内，绝不 500）。"""
    try:
        requests = client.counter_total("kquery_http_requests_total")
        errors = client.counter_total(
            "kquery_http_requests_total", '{status=~"5.."}'
        )
        p95 = client.quantile("kquery_http_latency_seconds", 0.95)
        cache_hit = client.counter_total("rag_cache_total", '{result="hit"}')
        cache_all = client.counter_total("rag_cache_total")
    except Exception as exc:  # noqa: BLE001 - 看板不该因为监控后端挂了就 500
        logger.warning("Prometheus 查询失败，回落进程内指标: %s", exc)
        return None
    return {
        "requests": requests,
        "errors": errors,
        "error_rate": round(errors / requests, 4) if requests else 0.0,
        "p95_latency_ms": round(p95 * 1000, 1),
        "cache_hit_rate": round(cache_hit / cache_all, 4) if cache_all else 0.0,
        "source": "prometheus",
    }


@router.get("/metrics/summary", response_model=Envelope)
def metrics_summary(request: Request, range_s: int = Query(default=3600, ge=60, le=86400)) -> Envelope:
    """监控大盘首屏指标。

    **数据源**：配了 `PROMETHEUS_URL` → 查 Prometheus（多副本下的真相源）；
    否则回落进程内 MetricsStore（单副本本地开发）。响应里的 `source` 字段
    说明用的是哪一个——**必须让看板知道自己在看哪个副本的数据**。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    client = _prom_client(request)
    data = None
    if client is not None:
        data = _summary_from_prometheus(client)
    if data is None:
        data = {**request.app.state.metrics_store.summary(range_s), "source": "process"}
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


@router.get("/metrics/timeseries", response_model=Envelope)
def metrics_timeseries(
    request: Request,
    metric: str = Query(default="requests"),
    range_s: int = Query(default=3600, ge=60, le=86400),
) -> Envelope:
    """监控时序曲线：指定指标在时间窗口上的采样点。

    数据源与 `/admin/metrics/summary` 一致（Prometheus 优先，回落进程内）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    client = _prom_client(request)
    points = None
    if client is not None:
        expr = {
            "requests": "sum(rate(kquery_http_requests_total[1m]))",
            "errors": 'sum(rate(kquery_http_requests_total{status=~"5.."}[1m]))',
            "cache_hit_rate": (
                'sum(rate(rag_cache_total{result="hit"}[1m]))'
                " / clamp_min(sum(rate(rag_cache_total[1m])), 0.001)"
            ),
            "p95_latency_ms": (
                "histogram_quantile(0.95, "
                "sum(rate(kquery_http_latency_seconds_bucket[1m])) by (le)) * 1000"
            ),
            "llm_calls": "sum(rate(rag_llm_calls_total[1m]))",
        }.get(metric)
        if expr:
            try:
                rows = client.range_query(expr, time.time() - range_s, time.time(), 60.0)
                points = [
                    {"ts": ts, "value": val}
                    for row in rows
                    for ts, val in (row.get("values") or [])
                ]
            except Exception as exc:  # noqa: BLE001 - Prometheus 区间查询失败，回落进程内
                logger.warning("Prometheus 区间查询失败，回落进程内: %s", exc)
                points = None
    source = "prometheus"
    if points is None:
        points = request.app.state.metrics_store.timeseries(metric, range_s)
        source = "process"
    return ok_envelope(
        {"metric": metric, "range_s": range_s, "points": points, "source": source},
        trace_id, elapsed_ms_since(started),
    )


@router.get("/metrics/top-queries", response_model=Envelope)
def metrics_top_queries(request: Request, limit: int = Query(default=10, ge=1, le=100)) -> Envelope:
    """高频问题排行（按查询次数）：用于发现「用户到底在问什么」。

    数据来自进程内 MetricsStore（多副本下是单副本视角）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(request.app.state.metrics_store.top_queries(limit), trace_id, elapsed_ms_since(started))


@router.get("/metrics/context", response_model=Envelope)
def metrics_context(request: Request) -> Envelope:
    """上下文构成统计（`ST-01` 阶段 A）。

    回答「最近的请求里，证据 / 工具 / 历史 / 记忆 / 系统各占多少 token，
    最常被裁掉的是什么」——用来标定各来源的配额比例。

    数据来自进程内 MetricsStore（多副本下是单副本视角）。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(request.app.state.metrics_store.context_summary(), trace_id, elapsed_ms_since(started))


@router.get("/metrics/agent", response_model=Envelope)
def metrics_agent(request: Request) -> Envelope:
    """Agent 执行指标：平均迭代/熔断率/工具分布/异常列表。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(request.app.state.metrics_store.agent_summary(), trace_id, elapsed_ms_since(started))


@router.get("/metrics/quality", response_model=Envelope)
def metrics_quality(request: Request) -> Envelope:
    """质量指标：Faithfulness / 拒答率 / 平均重生成 / CRAG 分支占比。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    data = request.app.state.metrics_store.quality_summary()
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


@router.get("/retrieval-config", response_model=Envelope)
def get_retrieval_config(request: Request) -> Envelope:
    """当前生效的检索配置（含灰度覆盖层与生效时间）。

    变更走 `PUT /admin/retrieval-config`；生效有延迟（`retrieval_config_apply_delay_s`）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(request.app.state.config_store.view(), trace_id, elapsed_ms_since(started))


class RetrievalConfigUpdateResult(BaseModel):
    effective_at: float
    message: str


@router.put("/retrieval-config", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def put_retrieval_config(config: RetrievalConfig, request: Request) -> Envelope:
    """保存检索参数：不立即生效，返回生效时刻供前端提示。"""
    trace_id = get_trace_id()
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


@router.post("/retrieval-config/reset", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def reset_retrieval_config(request: Request) -> Envelope:
    """把检索配置重置为默认值（保留一份默认快照，便于回滚到「出厂设置」）。

    返回里带 `effective_at`：重置同样受生效延迟约束，不是立即生效。"""
    trace_id = get_trace_id()
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
    """成本看板。按 phase/model/tenant 聚合，并可看消耗最高的问答。

    原先寄居在 `api/reports.py`，与多智能体报告无关，报告中心下线时随迁到此处。
    本 router 已带 `prefix="/admin"` 与整 router 的 admin 门禁，故不再重复声明。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    # 按需载入：成本台账仅在展示时用
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


# ---- 索引别名（重建不停机切换）----


class IndexAliasSwitchRequest(BaseModel):
    """切换索引别名指向的物理索引。

    - `physical`：目标物理索引名（目录名 / 集合名）；留空则切到"下一个"（`{base}_v{n+1}`）。
    - `rollback`：true 时回滚到上一个物理索引（重建把线上搞坏了时的退路）。
    """

    physical: str = ""
    rollback: bool = False


@router.get("/index/alias", response_model=Envelope)
def index_alias_status(request: Request) -> Envelope:
    """当前索引别名状态：别名、物理索引名、版本、上一版。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    alias = getattr(request.app.state, "index_alias", None)
    if alias is None:
        return ok_envelope({"available": False}, trace_id, elapsed_ms_since(started))
    return ok_envelope({"available": True, **alias.status()}, trace_id, elapsed_ms_since(started))


@router.post("/index/alias/switch", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def index_alias_switch(req: IndexAliasSwitchRequest, request: Request) -> Envelope:
    """原子切换索引别名（秒级，可回滚）。

    **不删旧物理索引**：切错了再切回去即可（回滚也是原子操作）。
    注意：切换只改"指针"；已加载的 store 实例仍指向旧物理索引，
    需要重建 store（或重启）才会真正读到新索引——这是刻意保留的一步，
    避免"切了一半"导致新旧索引混读。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    alias = getattr(request.app.state, "index_alias", None)
    if alias is None:
        return ok_envelope({"available": False}, trace_id, elapsed_ms_since(started))
    try:
        status = alias.rollback() if req.rollback else alias.switch(req.physical or None)
    except ValueError as exc:
        logger.warning("索引别名切换失败: %s", exc)
        return ok_envelope(
            {"available": True, "error": str(exc), **alias.status()},
            trace_id, elapsed_ms_since(started),
        )
    logger.info("索引别名已切换 admin trace_id=%s -> %s", trace_id, status.get("physical"))
    return ok_envelope({"available": True, **status}, trace_id, elapsed_ms_since(started))


# ---- 灰度：作用域检索配置（批次 6）----


class ScopedConfigRequest(BaseModel):
    """作用域检索配置（灰度）。

    - `scope`：`tenant:<id>` 或 `percent:<0-100>`
    - `config`：该作用域下生效的检索参数
    - `experiment_id`：实验标识（**只进日志与响应，不进指标 label**）
    - `enabled`：是否启用（false = 暂停灰度，配置保留）
    """

    scope: str
    config: RetrievalConfig
    experiment_id: str = ""
    enabled: bool = True


@router.get("/retrieval-config/scoped", response_model=Envelope)
def get_scoped_configs(request: Request) -> Envelope:
    """列出所有灰度作用域及其配置。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(
        {"scoped": request.app.state.config_store.scoped_view()},
        trace_id, elapsed_ms_since(started),
    )


@router.put("/retrieval-config/scoped", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def put_scoped_config(req: ScopedConfigRequest, request: Request) -> Envelope:
    """登记一个灰度作用域（租户定向 / 百分比分桶）。

    分桶按**稳定哈希**：同一用户永远落在同一组，实验数据才能归因。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    try:
        config = request.app.state.config_store.set_scoped(
            req.scope, req.config, enabled=req.enabled, experiment_id=req.experiment_id
        )
    except ValueError as exc:
        raise ApiError(ErrCode.INVALID_INPUT, str(exc), http_status=400) from exc
    logger.info(
        "灰度配置已登记 trace_id=%s scope=%s experiment=%s",
        trace_id, req.scope, req.experiment_id or "-",
    )
    return ok_envelope(
        {"scope": req.scope, "config": config, "experiment_id": req.experiment_id},
        trace_id, elapsed_ms_since(started),
    )


@router.delete("/retrieval-config/scoped", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def delete_scoped_config(scope: str, request: Request) -> Envelope:
    """移除一个灰度作用域（灰度结束/回滚）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    removed = request.app.state.config_store.clear_scoped(scope)
    return ok_envelope(
        {"scope": scope, "removed": removed},
        trace_id, elapsed_ms_since(started),
    )


# ---------- 工具目录 ----------

@router.get("/tools", response_model=Envelope)
def list_tools(request: Request) -> Envelope:
    """工具目录与健康状态。

    没有这个端点时，"某个工具挂了导致大量报告证据不足"只能靠翻日志发现。
    前端「工具」面板据此显示每个工具的可用性徽标。

    （本端点原在 `api/security.py`，随该文件一并迁到管理面 router：
    它是工具治理能力，不是权限管理能力。）

    `specs` 字段是**完整工具规格**（描述 / 危险级别 / 参数 schema），
    来自 `ToolRegistry.specs()`——该方法此前在生产路径上零调用点（死代码），
    现在由本端点消费（`A-21.8`）：排障时第一句要问的是「现在到底注册了哪些
    工具、各自的参数是什么」，静态文档对不上运行时注册表。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    registry = request.app.state.tool_registry
    rows = registry.health_report()
    specs = registry.specs()
    return ok_envelope(
        {
            "tools": rows,
            "total": len(rows),
            "tripped": [r["name"] for r in rows if r["tripped"]],
            "specs": specs,
        },
        trace_id, elapsed_ms_since(started),
    )
