"""企业护栏管理端点（阶段 9 前端规范 §九）。

- /admin/users：用户列表与编辑（静态用户目录，演示用）；
- /admin/documents/acl：ACL 列表 / 修改（返回影响人数，供前端风险提示）；
- /admin/audit：审计查询（过滤 + trace 聚合）与 CSV 导出（不含原文）；
- /admin/quota：配额总览与单租户配置；
- /admin/sensitive：敏感词表 CRUD（按租户隔离）；
- /admin/eval/*：评测集与运行（结果绑定评测集版本 + 模型版本）。

管理操作本身写审计（admin.action）——审计查询者也留痕。
"""
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["security"])


def _audit_admin(request: Request, trace_id: str, action: str, detail: dict) -> None:
    request.app.state.audit_store.log_admin(
        trace_id, request.headers.get("X-Tenant-Id", "default"),
        request.headers.get("X-User-Id", "anon"), action, detail,
    )


# ---------- 用户 ----------

@router.get("/admin/users", response_model=Envelope)
def list_users(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _audit_admin(request, trace_id, "users.list", {})
    return ok_envelope(request.app.state.user_directory, trace_id, elapsed_ms_since(started))


class UserEditRequest(BaseModel):
    roles: list[str] = []
    groups: list[str] = []
    enabled: bool = True


@router.put("/admin/users/{user_id}", response_model=Envelope)
def edit_user(user_id: str, req: UserEditRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    users: list[dict] = request.app.state.user_directory
    for u in users:
        if u["user_id"] == user_id:
            u.update({"roles": req.roles, "groups": req.groups, "enabled": req.enabled})
            _audit_admin(request, trace_id, "users.edit", {"user_id": user_id})
            return ok_envelope(u, trace_id, elapsed_ms_since(started))
    raise ApiError(ErrCode.FORBIDDEN, f"用户不存在: {user_id}", http_status=404)


# ---------- 文档 ACL ----------

@router.get("/admin/documents/acl", response_model=Envelope)
def list_acl(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    acl = request.app.state.acl_service
    docs = request.app.state.knowledge.list_files()
    _audit_admin(request, trace_id, "acl.list", {})
    return ok_envelope(acl.list_acl(docs), trace_id, elapsed_ms_since(started))


class SetAclRequest(BaseModel):
    allow: list[str] = []
    deny: list[str] = []
    is_public: bool = False


@router.put("/admin/documents/{doc_id}/acl", response_model=Envelope)
def set_acl(doc_id: str, req: SetAclRequest, request: Request) -> Envelope:
    """修改 ACL：返回变更影响人数（后端反查）与风险等级（前端二次确认依据）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    knowledge = request.app.state.knowledge
    knowledge.get(doc_id)  # 不存在 → 404
    acl = request.app.state.acl_service
    result = acl.set_acl(
        doc_id, req.allow, req.deny, req.is_public,
        updated_by=request.headers.get("X-User-Id", "anon"),
    )
    _audit_admin(request, trace_id, "acl.set", {
        "doc_id": doc_id, "risk": result["risk"],
        "added": result["impact"]["added_count"], "removed": result["impact"]["removed_count"],
    })
    return ok_envelope(result, trace_id, elapsed_ms_since(started))


# ---------- 审计 ----------

@router.get("/admin/audit", response_model=Envelope)
def query_audit(
    request: Request,
    range_s: int = Query(default=3600, ge=60, le=86400 * 30),
    event: str | None = None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    trace_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
) -> Envelope:
    my_trace = new_trace_id()
    set_trace_id(my_trace)
    started = now_ms()
    import time as _time

    records = request.app.state.audit_store.query(
        since=_time.time() - range_s, event=event, tenant_id=tenant_id,
        user_id=user_id, trace_id=trace_id, limit=limit,
    )
    _audit_admin(request, my_trace, "audit.query", {"count": len(records)})
    return ok_envelope(records, my_trace, elapsed_ms_since(started))


@router.get("/admin/audit/export")
def export_audit(request: Request, range_s: int = Query(default=86400)) -> PlainTextResponse:
    """导出 CSV（不含问题与答案原文，仅 hash + 元数据）。"""
    import time as _time

    my_trace = new_trace_id()
    _audit_admin(request, my_trace, "audit.export", {"range_s": range_s})
    csv_text = request.app.state.audit_store.export_csv(since=_time.time() - range_s)
    return PlainTextResponse(csv_text, media_type="text/csv")


# ---------- 配额 ----------

@router.get("/admin/quota", response_model=Envelope)
def quota_overview(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    quota = request.app.state.quota_service
    tenants = list({u["tenant_id"] for u in request.app.state.user_directory} | {"default"})
    return ok_envelope(quota.overview(tenants), trace_id, elapsed_ms_since(started))


@router.get("/admin/quota/platform", response_model=Envelope)
def platform_quota_overview(request: Request) -> Envelope:
    """平台侧额度余量（账号级上游约束，全租户共享）。

    这是唯一能提前预警"整个系统即将不可用"的指标：应用侧配额只影响单租户，
    而平台侧限流影响所有租户。同时给出各模型调用量与平台失败构成。
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    snapshot = request.app.state.platform_monitor.snapshot()
    return ok_envelope(snapshot, trace_id, elapsed_ms_since(started))


class QuotaConfigRequest(BaseModel):
    tenant_id: str
    per_minute: int = Field(ge=1, le=10000)
    per_day: int = Field(ge=10, le=10_000_000)


@router.put("/admin/quota", response_model=Envelope)
def set_quota(req: QuotaConfigRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    request.app.state.quota_service.set_policy(req.tenant_id, req.per_minute, req.per_day)
    _audit_admin(request, trace_id, "quota.set", {"tenant_id": req.tenant_id})
    return ok_envelope({"tenant_id": req.tenant_id, "per_minute": req.per_minute, "per_day": req.per_day}, trace_id, elapsed_ms_since(started))


# ---------- 敏感词 ----------

@router.get("/admin/sensitive", response_model=Envelope)
def list_sensitive(request: Request, tenant_id: str | None = None) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(
        request.app.state.sensitive_scanner.list_words(tenant_id), trace_id, elapsed_ms_since(started)
    )


class AddSensitiveWordRequest(BaseModel):
    word: str = Field(min_length=1, max_length=50)
    category: str = Field(pattern="^(机密|隐私|禁语|内部)$")
    action: str = Field(pattern="^(mask|refuse|tag)$")
    tenant_id: str = "default"
    enabled: bool = True


@router.post("/admin/sensitive", response_model=Envelope)
def add_sensitive(req: AddSensitiveWordRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    scanner = request.app.state.sensitive_scanner
    words = scanner.list_words()
    words.append(req.model_dump())
    scanner.save_words(words)
    _audit_admin(request, trace_id, "sensitive.add", {"category": req.category})  # 不记词条本身
    return ok_envelope({"added": True, "category": req.category}, trace_id, elapsed_ms_since(started))


@router.delete("/admin/sensitive/{word}", response_model=Envelope)
def delete_sensitive(word: str, request: Request, tenant_id: str = "default") -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    scanner = request.app.state.sensitive_scanner
    words = [w for w in scanner.list_words() if not (w["word"] == word and w.get("tenant_id", "default") == tenant_id)]
    scanner.save_words(words)
    _audit_admin(request, trace_id, "sensitive.delete", {"tenant_id": tenant_id})
    return ok_envelope({"deleted": True}, trace_id, elapsed_ms_since(started))


# ---------- 评测 ----------

class DatasetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    version: str = Field(min_length=1, max_length=20)
    samples: list[dict]


@router.post("/admin/eval/datasets", response_model=Envelope)
def save_dataset(req: DatasetRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    try:
        result = request.app.state.eval_store.save_dataset(req.name, req.version, req.samples)
    except ValueError as exc:
        raise ApiError(ErrCode.INVALID_REQUEST, str(exc), http_status=422) from exc
    return ok_envelope(result, trace_id, elapsed_ms_since(started))


@router.get("/admin/eval/datasets", response_model=Envelope)
def list_datasets(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(request.app.state.eval_store.list_datasets(), trace_id, elapsed_ms_since(started))


class EvalRunRequest(BaseModel):
    dataset: str
    dataset_version: str


@router.post("/admin/eval/run", response_model=Envelope)
def run_eval(req: EvalRunRequest, request: Request) -> Envelope:
    """触发评测（同步执行轻量指标；样本量大时应异步，此处演示规模同步即可）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    try:
        run = request.app.state.eval_runner.run(req.dataset, req.dataset_version)
    except FileNotFoundError as exc:
        raise ApiError(ErrCode.INVALID_REQUEST, str(exc), http_status=404) from exc
    _audit_admin(request, trace_id, "eval.run", {"run_id": run["run_id"], "dataset": req.dataset})
    return ok_envelope(
        {k: run[k] for k in ("run_id", "dataset", "dataset_version", "model_version",
                             "sample_count", "faithfulness", "citation_accuracy",
                             "refusal_accuracy", "created_at")},
        trace_id, elapsed_ms_since(started),
    )


@router.get("/admin/eval/runs", response_model=Envelope)
def list_runs(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    runs = request.app.state.eval_store.list_runs()
    return ok_envelope(
        [{k: r[k] for k in ("run_id", "dataset", "dataset_version", "model_version",
                            "sample_count", "faithfulness", "citation_accuracy",
                            "refusal_accuracy", "created_at")} for r in runs],
        trace_id, elapsed_ms_since(started),
    )


@router.get("/admin/eval/runs/{run_id}", response_model=Envelope)
def get_run(run_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    run = request.app.state.eval_store.get_run(run_id)
    if run is None:
        raise ApiError(ErrCode.INVALID_REQUEST, f"评测运行不存在: {run_id}", http_status=404)
    return ok_envelope(run, trace_id, elapsed_ms_since(started))
