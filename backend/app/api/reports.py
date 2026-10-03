"""报告中心端点（阶段 8 前端规范 §六）。

- POST /reports：提交报告任务（异步执行，立即返回 report_id；?sync=true 调试同步）；
- GET /reports：列表（状态/评分/轮次）；
- GET /reports/{id}：详情（含黑板快照与草稿）；
- GET /reports/{id}/plan | /evidence | /critique：分视图；
- DELETE /reports/{id}：删除（连同草稿与评审记录）；
- GET /admin/metrics/multiagent：多智能体指标（评审失败与未通过分开统计）。

核心契约：accepted=false 不是错误，走成功状态码（与阶段 5 拒答约定一致）。
"""
import logging

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.dependencies import get_orchestrator
from app.services.multiagent.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reports"])


class CreateReportRequest(BaseModel):
    goal: str = Field(min_length=5, max_length=2000)


def _get_report(request: Request, report_id: str) -> dict:
    report = request.app.state.report_store.get(report_id)
    if report is None:
        raise ApiError(ErrCode.REPORT_NOT_FOUND, f"报告不存在: {report_id}", http_status=404)
    return report


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
    tenant = request.headers.get("X-Tenant-Id", "default")
    user = request.headers.get("X-User-Id", "anon")
    if sync:
        # 同步执行（测试/调试）
        report = request.app.state.report_store.create(req.goal, tenant, user)
        result = orchestrator.run(report["report_id"], tenant)
    else:
        report = orchestrator.submit(req.goal, tenant, user)
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
    tenant = request.headers.get("X-Tenant-Id", "default")
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
    if not request.app.state.report_store.delete(report_id):
        raise ApiError(ErrCode.REPORT_NOT_FOUND, f"报告不存在: {report_id}", http_status=404)
    return ok_envelope({"deleted": True}, trace_id, elapsed_ms_since(started))


@router.get("/admin/metrics/multiagent", response_model=Envelope)
def multiagent_metrics(request: Request) -> Envelope:
    """多智能体指标（阶段 8 前端规范 §5）：评审失败与未通过分开计数（统计红线）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    reports = request.app.state.report_store.list()
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
