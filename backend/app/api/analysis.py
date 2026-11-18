"""Query 分析端点。

- GET /knowledge/query-analysis：分析历史（意图/是否检索/时间范围过滤）；
- GET /knowledge/query-analysis/{id}：单条详情（改写/子问题/路由全字段）；
- GET /knowledge/query-patterns：意图分布统计（Query 模式分析卡片）。
"""

from fastapi import APIRouter, Query, Request

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, get_trace_id, now_ms, set_trace_id
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/knowledge", tags=["query-analysis"])


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
    """查询预处理分析记录列表（意图判定 / 是否需要检索 / 改写与拆解结果）。

    支持按意图、是否需要检索、时间窗口过滤；`limit` 上限 200。
    记录只返回**当前主体**的（含用户原话，跨租户读取属于越权）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    principal = request.state.principal  # 中间件已为每个请求写入固定身份
    store = request.app.state.analysis_store
    records = store.list_records(
        intent=intent,
        needs_retrieval=needs_retrieval,
        range_s=range_s,
        limit=limit,
        tenant_id=principal.tenant_id,
    )
    return ok_envelope([_row(r) for r in records], trace_id, elapsed_ms_since(started))


@router.get("/query-analysis/{analysis_id}", response_model=Envelope)
def get_analysis(
    analysis_id: str,
    request: Request,
) -> Envelope:
    """单条预处理分析记录详情（按 `analysis_id`）。

    归属不符与不存在同返回 404（不泄漏存在性）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    principal = request.state.principal  # 中间件已为每个请求写入固定身份
    store = request.app.state.analysis_store
    record = store.get(analysis_id, tenant_id=principal.tenant_id)
    if record is None:
        raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, "分析记录不存在", http_status=404)
    return ok_envelope(record.model_dump(), trace_id, elapsed_ms_since(started))


@router.get("/query-patterns", response_model=Envelope)
def query_patterns(
    request: Request,
    range_s: int | None = Query(default=None, ge=60),
) -> Envelope:
    """高频查询模式聚合（归一化后的查询指纹 + 出现次数 + 命中率）。

    用于发现「哪类问题答不对」——指纹是不可反推原文的哈希。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    principal = request.state.principal  # 中间件已为每个请求写入固定身份
    store = request.app.state.analysis_store
    return ok_envelope(
        store.patterns(range_s=range_s, tenant_id=principal.tenant_id),
        trace_id,
        elapsed_ms_since(started),
    )
