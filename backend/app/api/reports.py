"""报告中心端点（阶段 8 前端规范 §六）。

- POST /reports：提交报告任务（异步执行，立即返回 report_id；?sync=true 调试同步）；
- GET /reports：列表（状态/评分/轮次）；
- GET /reports/{id}：详情（含黑板快照与草稿）；
- GET /reports/{id}/plan | /evidence | /critique：分视图；
- DELETE /reports/{id}：删除（连同草稿与评审记录）；
- GET /admin/metrics/multiagent：多智能体指标（评审失败与未通过分开统计）。

核心契约：accepted=false 不是错误，走成功状态码（与阶段 5 拒答约定一致）。
"""
import json
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.dependencies import acl_filter_of, get_orchestrator, get_principal, require_role
from app.services.multiagent.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# 报告端点需认证（普通用户）；/admin/metrics/multiagent 另行挂 admin 门禁
router = APIRouter(tags=["reports"], dependencies=[Depends(get_principal)])

#: 阶段 8 §4 的 8 类进度事件（顺序即编排器的执行顺序）
REPORT_STREAM_EVENTS = (
    "plan_ready", "task_start", "task_end", "evidence_added",
    "validate_done", "draft_ready", "critique_done", "finished",
)


class CreateReportRequest(BaseModel):
    goal: str = Field(min_length=5, max_length=2000)


def _get_report(request: Request, report_id: str) -> dict:
    report = request.app.state.report_store.get(report_id)
    if report is None:
        raise ApiError(ErrCode.REPORT_NOT_FOUND, f"报告不存在: {report_id}", http_status=404)
    tenant = request.state.principal.tenant_id
    report_tenant = report.get("tenant_id")
    if report_tenant is None:
        # 旧报告无归属字段：视为属主租户可见，首次读取时回填（兼容历史数据）
        report["tenant_id"] = tenant
    elif report_tenant != tenant:
        # 跨租户访问：返回 404 而非 403，避免据此探测报告是否存在
        raise ApiError(ErrCode.REPORT_NOT_FOUND, f"报告不存在: {report_id}", http_status=404)
    return report


@router.get("/reports/{report_id}/stream")
def stream_report(
    report_id: str,
    request: Request,
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> StreamingResponse:
    """报告生成进度流（stage-8 §4 的 8 类事件）。

    与 `POST /reports?sync=true` 的区别：那个跑完才返回一个完整结果，
    这里的每个阶段都实时推一条。前端据此渲染"规划中 → 检索中 → 校验中 →
    撰写中 → 评审中 → 完成"的时间轴，而不是每 1.5s 轮询一次。

    契约：
    - 报告已存在且处于终态时，先补一条 `finished` 再结束连接（幂等重连）；
    - `finished` 之后连接关闭——客户端据此停止重连；
    - 客户端断开时编排器继续跑完（事件 sink 被摘掉），不中断报告生成。
    """
    report = _get_report(request, report_id)
    terminal = {"accepted", "capped", "unreviewed", "failed"}

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"

    if report["status"] in terminal:
        # 已经是终态：补发一次 finished 后立即关闭，避免前端空等
        def replay():
            yield sse("finished", {
                "report_id": report_id, "status": report["status"],
                "accepted": report.get("accepted"),
                "critic_score": report.get("critic_score"),
                "rounds": report.get("rounds"),
                "unresolved_issues": report.get("unresolved_issues", []),
                "replayed": True,
            })
        return StreamingResponse(
            replay(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    tenant = report.get("tenant_id") or request.state.principal.tenant_id
    # TASK-142/163：报告路径与 /ask 走同一 ACL 收口，工具证据同样受权限约束
    acl_filter = acl_filter_of(request, request.state.principal)

    def event_source():
        try:
            for event, payload in orchestrator.run_stream(report_id, tenant, acl_filter):
                yield sse(event, payload)
        except ApiError as exc:
            yield sse("error", {
                "report_id": report_id, "code": int(exc.code), "message": exc.message,
            })
        except Exception:  # noqa: BLE001
            logger.exception("报告 %s 进度流异常", report_id)
            yield sse("error", {
                "report_id": report_id,
                "code": int(ErrCode.INTERNAL_ERROR),
                "message": "报告生成异常，请查看报告详情",
            })

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/reports", response_model=Envelope)
def create_report(
    req: CreateReportRequest,
    request: Request,
    sync: bool = Query(default=False),
    orchestrator: Orchestrator = Depends(get_orchestrator),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant = request.state.principal.tenant_id
    user = request.state.principal.user_id
    # TASK-142/163：报告路径与 /ask 走同一 ACL 收口，工具证据同样受权限约束
    acl_filter = acl_filter_of(request, request.state.principal)
    if sync:
        # 同步执行（测试/调试）
        report = request.app.state.report_store.create(req.goal, tenant, user)
        result = orchestrator.run(report["report_id"], tenant, acl_filter)
    else:
        report = orchestrator.submit(req.goal, tenant, user, acl_filter)
        result = report
    return ok_envelope(
        {
            "report_id": result["report_id"], "title": result["title"],
            "status": result["status"], "accepted": result.get("accepted"),
        },
        trace_id, elapsed_ms_since(started),
    )


@router.get("/reports", response_model=Envelope)
def list_reports(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant = request.state.principal.tenant_id
    return ok_envelope(request.app.state.report_store.list(tenant), trace_id, elapsed_ms_since(started))


@router.get("/reports/{report_id}", response_model=Envelope)
def report_detail(report_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    report = _get_report(request, report_id)
    return ok_envelope(report, trace_id, elapsed_ms_since(started))


@router.get("/reports/{report_id}/plan", response_model=Envelope)
def report_plan(report_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    report = _get_report(request, report_id)
    bb = report.get("blackboard") or {}
    return ok_envelope(
        {"tasks": bb.get("plan", []), "layers": report["layers"]},
        trace_id, elapsed_ms_since(started),
    )


@router.get("/reports/{report_id}/evidence", response_model=Envelope)
def report_evidence(report_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    report = _get_report(request, report_id)
    bb = report.get("blackboard") or {}
    records = bb.get("evidence", [])
    return ok_envelope(
        {
            "records": records,
            "total": len(records),
            "adopted": sum(1 for r in records if r.get("adopted") is True),
            "rejected": sum(1 for r in records if r.get("adopted") is False),
        },
        trace_id, elapsed_ms_since(started),
    )


@router.get("/reports/{report_id}/critique", response_model=Envelope)
def report_critique(report_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    report = _get_report(request, report_id)
    bb = report.get("blackboard") or {}
    return ok_envelope(
        {
            "rounds": bb.get("critiques", []),
            "final_score": report.get("critic_score"),
            "accepted": report.get("accepted"),
            "unresolved_issues": report.get("unresolved_issues", []),
        },
        trace_id, elapsed_ms_since(started),
    )


@router.delete("/reports/{report_id}", response_model=Envelope)
def delete_report(report_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _get_report(request, report_id)  # 归属校验：跨租户删除返回 404
    request.app.state.report_store.delete(report_id)
    return ok_envelope({"deleted": True}, trace_id, elapsed_ms_since(started))


@router.get("/admin/costs", response_model=Envelope, dependencies=[Depends(require_role("admin"))])
def cost_dashboard(
    request: Request,
    from_ts: float = Query(default=0.0, alias="from"),
    to_ts: float = Query(default=0.0, alias="to"),
    group_by: str = Query(default="phase", pattern="^(phase|model|tenant_id)$"),
    tenant_id: str = Query(default=""),
) -> Envelope:
    """成本看板（TASK-134）。按 phase/model/tenant 聚合，并可看消耗最高的问答。"""
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


@router.get("/admin/metrics/multiagent", response_model=Envelope, dependencies=[Depends(require_role("admin"))])
def multiagent_metrics(request: Request) -> Envelope:
    """多智能体指标（阶段 8 前端规范 §5）：评审失败与未通过分开计数（统计红线）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    reports = request.app.state.report_store.list(request.state.principal.tenant_id)
    details = [request.app.state.report_store.get(r["report_id"]) for r in reports]
    total = len(details)
    if not total:
        return ok_envelope(
            {
                "reports": 0, "by_status": {}, "avg_subtasks": 0,
                "avg_critic_rounds": 0, "critic_first_pass_rate": None,
                "layer_distribution": {}, "role_calls": {},
            },
            trace_id, elapsed_ms_since(started),
        )
    by_status: dict[str, int] = {}
    subtask_counts, round_counts, layer_hist = [], [], {}
    role_sum: dict[str, int] = {}
    first_round_pass = 0
    critiqued = 0
    for d in details:
        by_status[d["status"]] = by_status.get(d["status"], 0) + 1
        bb = d.get("blackboard") or {}
        subtask_counts.append(len(bb.get("plan", [])))
        if d.get("rounds"):
            round_counts.append(d["rounds"])
        for layer in {t.get("layer", 0) for t in bb.get("plan", [])}:
            layer_hist[str(layer)] = layer_hist.get(str(layer), 0) + 1
        for role, n in (d.get("role_calls") or {}).items():
            role_sum[role] = role_sum.get(role, 0) + n
        # Critic 一次通过率：第一轮即通过
        critiques = bb.get("critiques", [])
        if critiques:
            critiqued += 1
            if critiques[0]["score"] >= 8.0:
                first_round_pass += 1
    return ok_envelope(
        {
            "reports": total,
            "by_status": by_status,  # rejected / capped / unreviewed / failed 分开计数
            "avg_subtasks": round(sum(subtask_counts) / total, 2),
            "avg_critic_rounds": round(sum(round_counts) / max(len(round_counts), 1), 2),
            "critic_first_pass_rate": round(first_round_pass / max(critiqued, 1), 4),
            "layer_distribution": layer_hist,
            "role_calls": role_sum,
        },
        trace_id, elapsed_ms_since(started),
    )
