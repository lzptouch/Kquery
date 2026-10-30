"""知识库管理端点。

- GET/POST /admin/kb、PUT/DELETE /admin/kb/{kb_id}：知识库实体 CRUD；
- GET/PUT  /admin/kb/{kb_id}/grants：按用户授权（全量覆盖，返回影响人数与风险等级）；
- GET/PUT  /admin/kb/{kb_id}/documents：库内文档列表与批量移入；
- GET      /admin/users/{user_id}/kbs：反查某用户可用的知识库（用户管理页用）。

判定与存储见 services/security/kb.py（判定树与文档 ACL 同构）。
KB 授权生效于检索链路的兜底闭包（dependencies.acl_filter_of），
撤权即失效依赖缓存签名携带 KB 版本（main.py 装配处的复合版本）。
"""
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.dependencies import get_principal, require_role
from app.services.security.auth import Principal

logger = logging.getLogger(__name__)

# 本文件全部为 /admin/* 管理端点：整 router 挂 admin 门禁
router = APIRouter(tags=["kb"], dependencies=[Depends(require_role("admin"))])


def resolve_kb_for_write(request: Request, kb_id: str | None, tenant_id: str) -> str:
    """解析写路径的目标知识库：缺省归默认 KB；显式指定时校验存在性与租户匹配。

    防探测口径：KB 不存在与跨租户统一按「无权限或不存在」拒绝（与文档 ACL 同文案）。
    kb 服务未装配（kb_enabled=false）时不校验，行为退回现状。
    默认 KB 不做租户闸门：它是各租户各自的公共桶（新租户首次上传即隐式归位），
    跨租户泄漏由文档级租户闸门兜底。
    """
    if not kb_id:
        return "default"
    kb = getattr(request.app.state, "kb_service", None)
    if kb is None:
        return kb_id
    record = kb.get(kb_id)
    if record is None or (kb_id != "default" and record.tenant_id != tenant_id):
        raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
    return kb_id


def _kb_view(kb, doc_count: int, granted_count: int) -> dict:
    return {
        "kb_id": kb.kb_id,
        "name": kb.name,
        "description": kb.description,
        "tenant_id": kb.tenant_id,
        "is_public": kb.is_public,
        "created_by": kb.created_by,
        "created_at": kb.created_at,
        "updated_at": kb.updated_at,
        "doc_count": doc_count,
        "granted_count": granted_count,
    }


def _tenant_kbs(request: Request, principal: Principal):
    """只列本租户的 KB（admin ≠ 跨租户超级用户，KQ-02 同口径）。"""
    kb = request.app.state.kb_service
    return kb, kb.list_all(tenant_id=principal.tenant_id)


@router.get("/admin/kb", response_model=Envelope)
def list_kbs(request: Request, principal: Principal = Depends(get_principal)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    kb, kbs = _tenant_kbs(request, principal)
    knowledge = request.app.state.knowledge
    data = [
        _kb_view(k, knowledge.count_by_kb(k.kb_id), len(kb.grants_of(k.kb_id)))
        for k in kbs
    ]
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


class KbCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    is_public: bool = False


@router.post("/admin/kb", response_model=Envelope)
def create_kb(req: KbCreateRequest, request: Request, principal: Principal = Depends(get_principal)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    kb = request.app.state.kb_service
    record = kb.create(
        name=req.name.strip(),
        description=req.description,
        is_public=req.is_public,
        tenant_id=principal.tenant_id,
        created_by=principal.user_id,
    )
    return ok_envelope(_kb_view(record, 0, 0), trace_id, elapsed_ms_since(started))


class KbUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = None
    is_public: bool | None = None


@router.put("/admin/kb/{kb_id}", response_model=Envelope)
def update_kb(
    kb_id: str, req: KbUpdateRequest, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    kb = request.app.state.kb_service
    existing = kb.get(kb_id)
    if existing is None or existing.tenant_id != principal.tenant_id:
        raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
    patch = req.model_dump(exclude_none=True)
    record = kb.update(kb_id, patch, updated_by=principal.user_id)
    return ok_envelope(_kb_view(record, request.app.state.knowledge.count_by_kb(kb_id),
                                len(kb.grants_of(kb_id))), trace_id, elapsed_ms_since(started))


@router.delete("/admin/kb/{kb_id}", response_model=Envelope)
def delete_kb(kb_id: str, request: Request, principal: Principal = Depends(get_principal)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    kb = request.app.state.kb_service
    existing = kb.get(kb_id)
    if existing is None or existing.tenant_id != principal.tenant_id:
        raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
    kb.delete(kb_id, doc_count=request.app.state.knowledge.count_by_kb(kb_id))
    return ok_envelope({"kb_id": kb_id, "deleted": True}, trace_id, elapsed_ms_since(started))


# ---------- 授权 ----------


class KbGrantsRequest(BaseModel):
    user_ids: list[str] = Field(default_factory=list, max_length=500)


@router.get("/admin/kb/{kb_id}/grants", response_model=Envelope)
def list_grants(kb_id: str, request: Request, principal: Principal = Depends(get_principal)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    kb = request.app.state.kb_service
    existing = kb.get(kb_id)
    if existing is None or existing.tenant_id != principal.tenant_id:
        raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
    return ok_envelope(kb.grants_of(kb_id), trace_id, elapsed_ms_since(started))


@router.put("/admin/kb/{kb_id}/grants", response_model=Envelope)
def set_grants(
    kb_id: str, req: KbGrantsRequest, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """全量覆盖授权：返回变更影响人数（后端反查）与风险等级（前端二次确认依据）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    kb = request.app.state.kb_service
    existing = kb.get(kb_id)
    if existing is None or existing.tenant_id != principal.tenant_id:
        raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
    result = kb.set_grants(kb_id, req.user_ids, granted_by=principal.user_id)
    return ok_envelope(result, trace_id, elapsed_ms_since(started))


# ---------- 库内文档 ----------


@router.get("/admin/kb/{kb_id}/documents", response_model=Envelope)
def list_kb_documents(kb_id: str, request: Request, principal: Principal = Depends(get_principal)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    kb = request.app.state.kb_service
    existing = kb.get(kb_id)
    if existing is None or existing.tenant_id != principal.tenant_id:
        raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
    knowledge = request.app.state.knowledge
    docs = [
        d for d in knowledge.list_files(tenant_id=principal.tenant_id)
        if (d.knowledge_base_id or "default") == kb_id
    ]
    return ok_envelope(
        [{"doc_id": d.doc_id, "filename": d.filename, "status": d.status,
          "chunks": d.chunk_count, "uploaded_at": d.uploaded_at} for d in docs],
        trace_id, elapsed_ms_since(started),
    )


class KbMoveRequest(BaseModel):
    doc_ids: list[str] = Field(min_length=1, max_length=500)


@router.put("/admin/kb/{kb_id}/documents", response_model=Envelope)
def move_documents(
    kb_id: str, req: KbMoveRequest, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """批量移入文档。ACL 挂 doc_id 天然跟随，不重授权、不动索引。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    kb = request.app.state.kb_service
    existing = kb.get(kb_id)
    if existing is None or existing.tenant_id != principal.tenant_id:
        raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
    knowledge = request.app.state.knowledge
    moved, skipped = [], []
    for doc_id in req.doc_ids:
        try:
            record = knowledge.get(doc_id)
        except ApiError:
            skipped.append(doc_id)
            continue
        if record.tenant_id != principal.tenant_id:
            skipped.append(doc_id)
            continue
        knowledge.move_to_kb(doc_id, kb_id)
        moved.append(doc_id)
    return ok_envelope({"kb_id": kb_id, "moved": moved, "skipped": skipped},
                       trace_id, elapsed_ms_since(started))


# ---------- 反查（用户管理页）----------


@router.get("/admin/users/{user_id}/kbs", response_model=Envelope)
def kbs_of_user(user_id: str, request: Request, principal: Principal = Depends(get_principal)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    directory = request.app.state.user_directory
    record = directory.get(user_id)
    if record is None:
        raise ApiError(ErrCode.FORBIDDEN, "无权限或不存在", http_status=404)
    kb = request.app.state.kb_service
    kbs = kb.kbs_of_user(user_id, record.get("tenant_id") or "default")
    return ok_envelope(
        [{"kb_id": k.kb_id, "name": k.name, "is_public": k.is_public} for k in kbs],
        trace_id, elapsed_ms_since(started),
    )
