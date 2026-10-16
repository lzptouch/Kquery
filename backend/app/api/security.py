"""企业护栏管理端点（阶段 9 前端规范 §九）。

- /admin/users：用户列表与编辑（静态用户目录，演示用）；
- /admin/documents/acl：ACL 列表 / 修改（返回影响人数，供前端风险提示）；
- /admin/audit：审计查询（过滤 + trace 聚合）与 CSV 导出（不含原文）；
- /admin/quota：配额总览与单租户配置；
- /admin/sensitive：敏感词表 CRUD（按租户隔离）；
- /admin/eval/*：评测集与运行（结果绑定评测集版本 + 模型版本 + 业务代码版本）
  以及两次运行的逐指标对比；
- /admin/users/permission-matrix：角色 × 能力矩阵（前端 PermissionMatrix 组件的数据源）。

管理操作本身写审计（admin.action）——审计查询者也留痕。
"""
import logging

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.dependencies import require_role

logger = logging.getLogger(__name__)

# 本文件全部为 /admin/* 管理端点：整 router 挂 admin 门禁
router = APIRouter(tags=["security"], dependencies=[Depends(require_role("admin"))])


def _audit_admin(request: Request, trace_id: str, action: str, detail: dict) -> None:
    # 审计身份以认证主体为准：认证开启后请求头仍可伪造，principal 才可作审计依据
    principal = request.state.principal
    request.app.state.audit_store.log_admin(
        trace_id, principal.tenant_id, principal.user_id, action, detail,
    )


# ---------- 用户 ----------

@router.get("/admin/users", response_model=Envelope)
def list_users(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _audit_admin(request, trace_id, "users.list", {})
    directory = request.app.state.user_directory
    users = directory.all() if hasattr(directory, "all") else list(directory)
    return ok_envelope(users, trace_id, elapsed_ms_since(started))


class UserEditRequest(BaseModel):
    roles: list[str] = []
    groups: list[str] = []
    enabled: bool = True


@router.put("/admin/users/{user_id}", response_model=Envelope)
def edit_user(user_id: str, req: UserEditRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    directory = request.app.state.user_directory
    patch = {"roles": req.roles, "groups": req.groups, "enabled": req.enabled}
    if hasattr(directory, "upsert"):
        existing = directory.get(user_id)
        if existing is None:
            # 防探测：不存在与无权限统一为 12xx + 同一句文案
            raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
        updated = directory.upsert(user_id, patch)
    else:  # 兼容旧静态目录
        users: list[dict] = directory
        target = next((u for u in users if u["user_id"] == user_id), None)
        if target is None:
            raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
        target.update(patch)
        updated = target
    _audit_admin(request, trace_id, "users.edit", {"user_id": user_id})
    return ok_envelope(updated, trace_id, elapsed_ms_since(started))


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
        updated_by=request.state.principal.user_id,
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
    directory = request.app.state.user_directory
    known = (
        directory.tenants()
        if hasattr(directory, "tenants")
        else {u["tenant_id"] for u in directory}
    )
    tenants = sorted(set(known) | {settings_default_tenant(request)})
    return ok_envelope(quota.overview(tenants), trace_id, elapsed_ms_since(started))


def settings_default_tenant(request: Request) -> str:
    return request.app.state.settings.default_tenant_id


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


# 运行摘要字段：三个版本（评测集/模型/业务代码）缺一，指标变化就无法归因（§6.3）。
# 用 .get 而非下标：存量 run-*.json 里没有 code_version / coverage，硬取会 500。
_EVAL_RUN_FIELDS = (
    "run_id", "dataset", "dataset_version", "model_version", "code_version", "path",
    "sample_count", "faithfulness", "citation_accuracy", "refusal_accuracy", "coverage",
    "created_at",
)


def _eval_summary(run: dict) -> dict:
    return {key: run.get(key) for key in _EVAL_RUN_FIELDS}


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
    _audit_admin(request, trace_id, "eval.run", {
        "run_id": run["run_id"], "dataset": req.dataset,
        "code_version": run.get("code_version"),
    })
    return ok_envelope(_eval_summary(run), trace_id, elapsed_ms_since(started))


@router.get("/admin/eval/runs", response_model=Envelope)
def list_runs(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    runs = request.app.state.eval_store.list_runs()
    return ok_envelope([_eval_summary(r) for r in runs], trace_id, elapsed_ms_since(started))


@router.get("/admin/eval/compare", response_model=Envelope)
def compare_eval_runs(
    request: Request,
    a: str = Query(min_length=1, max_length=64),
    b: str = Query(min_length=1, max_length=64),
) -> Envelope:
    """两次运行的逐指标对比 + 失败样本差异（前端"v1.0 vs v1.2.0 ↑+3.5%"的数据源）。

    单独一个端点而不是让前端拉两次 run 自己算：差值口径（样本对齐、失败判定阈值）
    必须只有一份实现，否则两次"对比"会给出不同结论。
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    try:
        result = request.app.state.eval_store.compare(a, b)
    except FileNotFoundError as exc:
        raise ApiError(ErrCode.INVALID_REQUEST, str(exc), http_status=404) from exc
    _audit_admin(request, trace_id, "eval.compare", {"a": a, "b": b})
    return ok_envelope(result, trace_id, elapsed_ms_since(started))


@router.get("/admin/eval/runs/{run_id}", response_model=Envelope)
def get_run(run_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    run = request.app.state.eval_store.get_run(run_id)
    if run is None:
        raise ApiError(ErrCode.INVALID_REQUEST, f"评测运行不存在: {run_id}", http_status=404)
    return ok_envelope(run, trace_id, elapsed_ms_since(started))


@router.get("/admin/tools", response_model=Envelope, dependencies=[Depends(require_role("admin"))])
def list_tools(request: Request) -> Envelope:
    """工具目录与健康状态（TASK-109）。

    没有这个端点时，"某个工具挂了导致大量报告证据不足"只能靠翻日志发现。
    前端「工具」面板据此显示每个工具的可用性徽标。
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    registry = request.app.state.tool_registry
    rows = registry.health_report()
    return ok_envelope(
        {
            "tools": rows,
            "total": len(rows),
            "tripped": [r["name"] for r in rows if r["tripped"]],
        },
        trace_id, elapsed_ms_since(started),
    )


# ---------- 权限矩阵 ----------

# 能力清单：enforced 说明这一列到底由谁把守，`by` 给出**把守它的端点**。
#   backend  = 后端端点/护栏真正强制；
#   acl      = 由文档 ACL 身份表达式（role:/group:/user:）决定。
# TASK-164：原先 6 项标 `declared`（"管理台声明的约定，后端未做门禁"）。
# 那时是事实——`/admin/*` 端点完全没有角色校验。TASK-160 给全部 28 个
# `/admin/*` 端点挂上 `require_role("admin")` 之后，这 6 项**已经是真的**，
# 继续标 declared 反而低估了实际防护等级。这里改为 backend 并注明把守端点，
# 让运维能顺着 `by` 去核对，而不是只能相信这个字符串。
CAPABILITIES: list[dict] = [
    {"key": "ask", "label": "提问", "enforced": "backend", "by": "/ask"},
    {"key": "view_own_data", "label": "看自己数据", "enforced": "backend", "by": "Principal 租户/用户过滤"},
    {"key": "view_all_tenant_data", "label": "看全员数据", "enforced": "acl", "by": "文档 ACL 身份表达式"},
    {"key": "ingest_document", "label": "文档入库", "enforced": "backend", "by": "/knowledge/ingest"},
    {"key": "manage_acl", "label": "文档 ACL 管理", "enforced": "backend", "by": "/admin/documents/{doc_id}/acl"},
    {"key": "manage_users", "label": "用户管理", "enforced": "backend", "by": "/admin/users"},
    {"key": "manage_quota", "label": "配额管理", "enforced": "backend", "by": "/admin/quota"},
    {"key": "view_audit", "label": "审计查询", "enforced": "backend", "by": "/admin/audit"},
    {"key": "manage_sensitive", "label": "敏感词管理", "enforced": "backend", "by": "/admin/sensitive"},
    {"key": "run_eval", "label": "评测运行", "enforced": "backend", "by": "/admin/eval"},
]
_ALL_CAPABILITIES = tuple(c["key"] for c in CAPABILITIES)
_ASK_CAPABILITIES = ("ask", "view_own_data")
ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "admin": _ALL_CAPABILITIES,
    "hr": _ASK_CAPABILITIES + ("view_all_tenant_data", "ingest_document"),
    "finance": _ASK_CAPABILITIES + ("view_all_tenant_data",),
    "sales": _ASK_CAPABILITIES,
}
DEFAULT_CAPABILITIES = _ASK_CAPABILITIES  # 未分配角色的账号：只有提问与看自己数据
DEFAULT_ROLE_LABEL = "(未分配角色)"


@router.get("/admin/users/permission-matrix", response_model=Envelope)
def permission_matrix(request: Request) -> Envelope:
    """角色 × 能力矩阵（前端设计 §2.3 / PermissionMatrix 组件的数据源）。

    为什么必须由后端给：角色与能力是一份策略声明，写在前端就等于让界面自己定义权限，
    改权限要改两处且必然不同步。矩阵同时回带每个角色下的真实用户数——
    管理员对着一个零成员角色做授权决策，本身就是错的。
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    directory = request.app.state.user_directory
    users = directory.all() if hasattr(directory, "all") else list(directory)
    counts: dict[str, int] = {}
    for user in users:
        if not user.get("enabled", True):
            continue
        roles = user.get("roles") or []
        for role in roles:
            counts[role] = counts.get(role, 0) + 1
        if not roles:
            counts[DEFAULT_ROLE_LABEL] = counts.get(DEFAULT_ROLE_LABEL, 0) + 1

    matrix = []
    for role in sorted({*ROLE_CAPABILITIES, *counts}):
        granted = ROLE_CAPABILITIES.get(role, DEFAULT_CAPABILITIES)
        matrix.append({
            "role": role,
            "user_count": counts.get(role, 0),
            # 可访问性要求（前端设计 §12）：可/不可不能只靠颜色区分，
            # 必须同时给文字标记，granted/denied 就是给渲染用的第二通道。
            "capabilities": {c["key"]: c["key"] in granted for c in CAPABILITIES},
            "granted": [c["key"] for c in CAPABILITIES if c["key"] in granted],
            "denied": [c["key"] for c in CAPABILITIES if c["key"] not in granted],
        })
    _audit_admin(request, trace_id, "users.permission_matrix", {})
    return ok_envelope(
        {
            "capabilities": CAPABILITIES,
            "roles": [row["role"] for row in matrix],
            "matrix": matrix,
            "default_role": DEFAULT_ROLE_LABEL,
            "unassigned_capabilities": list(DEFAULT_CAPABILITIES),
        },
        trace_id,
        elapsed_ms_since(started),
    )
