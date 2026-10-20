"""知识库端点。

- POST /knowledge/upload：单文件上传并同步完成解析与索引，返回最终状态；
- GET  /knowledge/files：知识库列表（含块数——用户判断入库成功的直接证据）；
- POST /knowledge/rebuild/{doc_id}：重新构建索引；
- DELETE /knowledge/files/{doc_id}：删除文档及其块。
"""
import logging
import uuid

from fastapi import APIRouter, Depends, Request, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.upload_util import sanitize_filename, stream_to_disk
from app.core.envelope import Envelope, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.core.envelope import ApiError, ErrCode
from app.dependencies import (
    audit_admin,
    enforce_rate_limit,
    get_knowledge_service,
    get_principal,
    require_role,
)
from app.domain.schemas import DOC_STATUS_INDEXED
from app.services.knowledge import KnowledgeService
from app.services.security.auth import Principal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["knowledge"], dependencies=[Depends(require_role("admin"))])


def _assert_doc_tenant(principal: Principal, record) -> None:
    """按 id 取资源必须先断言租户（KQ-02）：跨租户一律 404，不做存在性探测。"""
    if record.tenant_id != principal.tenant_id:
        raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, f"文档不存在: {record.doc_id}", http_status=404)


def _file_view(record) -> dict:
    return {
        "file_id": record.file_id,
        "filename": record.filename,
        "doc_id": record.doc_id,
        "size": record.size,
        "uploaded_at": record.uploaded_at,
        "chunks": record.chunk_count,
        "status": record.status,
        "error": record.error,
    }


@router.post("/upload", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
async def upload(
    file: UploadFile,
    request: Request,
    service: KnowledgeService = Depends(get_knowledge_service),
    principal: Principal = Depends(get_principal),
) -> Envelope:
    """单文件上传并同步完成解析与索引。

    流式纪律（KQ-05）：边写边计数、超限即中断，不把整文件读进内存；
    分钟级的解析/索引放到线程池，不阻塞事件循环。
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    settings = request.app.state.settings
    filename = sanitize_filename(file.filename or "")
    tmp_path = settings.upload_dir / f".uploading-{uuid.uuid4().hex[:12]}.tmp"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    await stream_to_disk(file, tmp_path, settings.max_upload_mb)
    record = await run_in_threadpool(
        service.ingest_path, filename, tmp_path, None, None, principal.tenant_id
    )
    audit_admin(request, "knowledge.upload", {
        "doc_id": record.doc_id, "filename": filename, "status": record.status,
    })
    return ok_envelope(_file_view(record), trace_id, elapsed_ms_since(started))


@router.get("/files", response_model=Envelope)
def list_files(
    request: Request,
    service: KnowledgeService = Depends(get_knowledge_service),
    principal: Principal = Depends(get_principal),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    # KQ-02：管理面列表也必须按主体租户过滤（admin ≠ 跨租户超级用户）
    files = service.list_files(tenant_id=principal.tenant_id)
    return ok_envelope([_file_view(r) for r in files], trace_id, elapsed_ms_since(started))


@router.post("/rebuild/{doc_id}", response_model=Envelope)
def rebuild(
    doc_id: str,
    request: Request,
    service: KnowledgeService = Depends(get_knowledge_service),
    principal: Principal = Depends(get_principal),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _assert_doc_tenant(principal, service.get(doc_id))
    record = service.rebuild(doc_id)
    audit_admin(request, "knowledge.rebuild", {"doc_id": doc_id, "status": record.status})
    return ok_envelope(_file_view(record), trace_id, elapsed_ms_since(started))


@router.delete("/files/{doc_id}", response_model=Envelope)
def delete_file(
    doc_id: str,
    request: Request,
    service: KnowledgeService = Depends(get_knowledge_service),
    principal: Principal = Depends(get_principal),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _assert_doc_tenant(principal, service.get(doc_id))
    record = service.delete(doc_id)
    # 删除是不可逆动作，必须留痕（KQ-24）
    audit_admin(request, "knowledge.delete", {"doc_id": doc_id, "filename": record.filename})
    return ok_envelope(_file_view(record), trace_id, elapsed_ms_since(started))


# ---------- 预览 / 高级操作 / 诊断 ----------


@router.get("/preview/{doc_id}", response_model=Envelope)
def preview_document(
    doc_id: str, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """文档解析预览：大纲 + 块列表 + 表格 + 资源 + RAPTOR 树。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: KnowledgeService = request.app.state.knowledge
    record = service.get(doc_id)
    _assert_doc_tenant(principal, record)
    chunks = service.chunks_of(doc_id)
    tables = service.tables_of(doc_id)
    assets = service.assets_of(doc_id)
    # RAPTOR 树：按 level + parent_id 组织（叶子 ↔ 摘要节点）
    by_id = {c.chunk_id: c for c in chunks}
    raptor_tree = []
    for c in chunks:
        if c.level > 0:  # 摘要节点
            children = [x.chunk_id for x in chunks if x.parent_id == c.chunk_id]
            raptor_tree.append(
                {
                    "chunk_id": c.chunk_id,
                    "level": c.level,
                    "content": c.content,
                    "children": children,
                    "child_count": len(children),
                }
            )
    data = {
        "doc": _file_view(record),
        "outline": service.outline_of(doc_id),
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "seq": c.seq,
                "preview": c.content[:50],
                "chunker": c.chunker,
                "level": c.level,
                "parent_id": c.parent_id,
                "modality": c.modality,
                "source": c.source,
                "is_short": c.is_short,
            }
            for c in sorted(chunks, key=lambda x: (x.level, x.seq))
        ],
        "tables": [t.model_dump() for t in tables],
        "assets": [a.model_dump() for a in assets],
        "parents": [
            {
                "chunk_id": p.chunk_id,
                "seq": p.seq,
                "preview": p.content[:50],
                "chunker": p.chunker,
                "level": p.level,
                "parent_id": "",
                "modality": p.modality,
                "source": p.source,
                "is_short": False,
                "content": p.content,
            }
            for p in service.parents_of(doc_id)
        ],
        "raptor_tree": raptor_tree,
        "chunk_index": list(by_id.keys()),
    }
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


@router.get("/preview/{doc_id}/chunk/{chunk_id}", response_model=Envelope)
def preview_chunk(
    doc_id: str, chunk_id: str, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """单块详情：内容 + 位置 + 切块器 + 父块 + 子块。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: KnowledgeService = request.app.state.knowledge
    _assert_doc_tenant(principal, service.get(doc_id))
    store = request.app.state.store
    docstore = request.app.state.docstore
    chunk = store.get_chunk(chunk_id)
    if chunk is None:
        # 父块存于 DocStore 而非向量库（父子分块的双层结构）
        parent_chunk = docstore.get_parent(chunk_id)
        if parent_chunk is not None and parent_chunk.doc_id == doc_id:
            children = [
                {"chunk_id": c.chunk_id, "preview": c.content[:50]}
                for c in service.chunks_of(doc_id)
                if c.parent_id == parent_chunk.chunk_id
            ]
            return ok_envelope(
                {
                    "chunk_id": parent_chunk.chunk_id,
                    "content": parent_chunk.content,
                    "position": parent_chunk.source,
                    "chunker": parent_chunk.chunker,
                    "level": parent_chunk.level,
                    "modality": parent_chunk.modality,
                    "parent_id": "",
                    "parent_content": None,
                    "children": children,
                    "is_short": False,
                },
                trace_id,
                elapsed_ms_since(started),
            )
    if chunk is None or chunk.doc_id != doc_id:
        raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, f"块不存在: {chunk_id}", http_status=404)
    parent = docstore.get_parent(chunk.parent_id) if chunk.parent_id else None
    children = [
        {"chunk_id": c.chunk_id, "preview": c.content[:50]}
        for c in service.chunks_of(doc_id)
        if c.parent_id == chunk.chunk_id
    ]
    data = {
        "chunk_id": chunk.chunk_id,
        "content": chunk.content,
        "position": chunk.source,
        "chunker": chunk.chunker,
        "level": chunk.level,
        "modality": chunk.modality,
        "parent_id": chunk.parent_id,
        "parent_content": parent.content if parent else None,
        "children": children,
        "is_short": chunk.is_short,
    }
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


class ReparseRequest(BaseModel):
    chunk_policy: dict = Field(default_factory=dict)
    parse_policy: dict = Field(default_factory=dict)


@router.post("/{doc_id}/reparse", response_model=Envelope)
def reparse_document(
    doc_id: str, req: ReparseRequest, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """重新解析（新策略生成新版本块集合）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    from app.services.chunkers.factory import validate_chunk_policy

    if error := validate_chunk_policy(req.chunk_policy):
        raise ApiError(ErrCode.CHUNK_POLICY_INVALID, error)
    service: KnowledgeService = request.app.state.knowledge
    _assert_doc_tenant(principal, service.get(doc_id))
    record = service.reparse(doc_id, req.chunk_policy, req.parse_policy)
    audit_admin(request, "knowledge.reparse", {"doc_id": doc_id, "status": record.status})
    return ok_envelope(_file_view(record), trace_id, elapsed_ms_since(started))


@router.post("/{doc_id}/offline", response_model=Envelope)
def offline_document(
    doc_id: str, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """删除索引（临时下架）：保留源文件与记录，文档从检索中消失。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: KnowledgeService = request.app.state.knowledge
    _assert_doc_tenant(principal, service.get(doc_id))
    record = service.offline(doc_id)
    audit_admin(request, "knowledge.offline", {"doc_id": doc_id})
    return ok_envelope(_file_view(record), trace_id, elapsed_ms_since(started))


class RetrievalProbeRequest(BaseModel):
    doc_id: str = Field(min_length=1)
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=10)


@router.post("/retrieval-probe", response_model=Envelope)
def retrieval_probe(
    req: RetrievalProbeRequest, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """单文档检索诊断（契约端点，与 /knowledge/{doc_id}/probe 等价）。"""
    return probe_document(
        req.doc_id, ProbeRequest(query=req.query, top_k=req.top_k), request, principal
    )


@router.get("/stats", response_model=Envelope)
def knowledge_stats(
    request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """知识库 KPI 看板。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: KnowledgeService = request.app.state.knowledge
    files = service.list_files(tenant_id=principal.tenant_id)
    indexed = [f for f in files if f.status == DOC_STATUS_INDEXED]
    summary = request.app.state.metrics_store.summary(86400)
    data = {
        "documents": len(files),
        "indexed_documents": len(indexed),
        "total_chunks": sum(f.chunk_count for f in indexed),
        "avg_retrieval_ms": summary["p50_latency_ms"],
        "last_indexed_at": max((f.uploaded_at for f in indexed), default=None),
    }
    return ok_envelope(data, trace_id, elapsed_ms_since(started))


class ProbeRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=4, ge=1, le=10)


@router.post("/{doc_id}/probe", response_model=Envelope)
def probe_document(
    doc_id: str, req: ProbeRequest, request: Request, principal: Principal = Depends(get_principal)
) -> Envelope:
    """单文档检索诊断：返回该文档被命中的块列表。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    service: KnowledgeService = request.app.state.knowledge
    record = service.get(doc_id)  # 不存在 → 404
    _assert_doc_tenant(principal, record)
    hybrid = request.app.state.hybrid
    config = request.app.state.config_store.get_active()
    config = config.model_copy(update={"top_k": req.top_k})
    # KQ-02：检索租户取调用主体，而不是记录上的租户——
    # 用记录租户等于替其它租户做检索探针
    result = hybrid.search(req.query, principal.tenant_id, config, doc_id=doc_id)
    hits = [
        {
            "chunk_id": c.chunk.chunk_id,
            "seq": c.chunk.seq,
            "score": round(c.rerank_score if result.reranked else c.rrf_score, 4),
            "source_route": c.source_route,
            "source": c.chunk.source,
            "content": c.chunk.content,
        }
        for c in result.candidates
    ]
    return ok_envelope(
        {
            "doc_id": doc_id,
            "query": req.query,
            "reranked": result.reranked,
            "stats": result.stats.model_dump(),
            "hits": hits,
        },
        trace_id,
        elapsed_ms_since(started),
    )
