"""Query 分析端点（阶段 4 前端规范 §4 / §6）。

- GET /knowledge/query-analysis：分析历史（意图/是否检索/时间范围过滤）；
- GET /knowledge/query-analysis/{id}：单条详情（改写/子问题/路由全字段）；
- GET /knowledge/query-patterns：意图分布统计（Query 模式分析卡片）。
"""
import logging

from fastapi import APIRouter, Depends, Query, Request

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.dependencies import get_principal, require_role
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["query-analysis"], dependencies=[Depends(require_role("admin"))])


def _row(r) -> dict:
    return {
        "analysis_id": r.analysis_id,
        "created_at": r.created_at,
        "raw_query": r.raw_query,
        "tenant_id": r.tenant_id,
        "intent": r.intent,
        "needs_retrieval": r.needs_retrieval,
        "rewrite_count": len(r.rewrites),
        "sub_question_count": len(r.sub_questions),
        "elapsed_ms": r.elapsed_ms,
        "degraded": r.degraded,
    }


@router.get("/query-analysis", response_model=Envelope)
def list_analysis(
    request: Request,
    intent: str | None = Query(default=None),
    needs_retrieval: bool | None = Query(default=None),
    range_s: int | None = Query(default=None, ge=60),
    limit: int = Query(default=50, ge=1, le=200),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = request.app.state.analysis_store
    records = store.list_records(intent=intent, needs_retrieval=needs_retrieval, range_s=range_s, limit=limit)
    return ok_envelope([_row(r) for r in records], trace_id, elapsed_ms_since(started))


@router.get("/query-analysis/{analysis_id}", response_model=Envelope)
def get_analysis(analysis_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = request.app.state.analysis_store
    record = store.get(analysis_id)
    if record is None:
        raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, f"分析记录不存在: {analysis_id}", http_status=404)
    return ok_envelope(record.model_dump(), trace_id, elapsed_ms_since(started))


@router.get("/query-patterns", response_model=Envelope)
def query_patterns(request: Request, range_s: int | None = Query(default=None, ge=60)) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = request.app.state.analysis_store
    return ok_envelope(store.patterns(range_s=range_s), trace_id, elapsed_ms_since(started))
