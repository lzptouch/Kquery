"""图谱与多模态检索端点（阶段 7 前端规范 §六）。

- GET /graph/entities：实体列表（搜索 + 类型过滤）；
- GET /graph/entity/{name}：实体详情（入边/出边带方向）；
- GET /graph/subgraph：子图（画布渲染，truncated 截断提示）；
- GET /graph/communities：社区列表与摘要；
- POST /graph/build：从已有块构建/重建图谱（显式触发，成本可控）；
- GET /graph/stats：图谱健康度指标（孤立实体占比等）；
- POST /search/media：以文搜图；
- GET /assets/{name}：图像资源服务（前端渲染用，路径安全校验）。
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["graph"])
SUBGRAPH_MAX_EDGES = 100  # 子图截断上限（超出必须提示）


def _graph_store(request: Request):
    return request.app.state.graph_store


@router.get("/graph/entities", response_model=Envelope)
def list_entities(
    request: Request,
    q: str = Query(default="", max_length=100),
    types: str = Query(default=""),
) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = _graph_store(request)
    allowed = {t.strip() for t in types.split(",") if t.strip()}
    entities = []
    for e in store.all_entities():
        if allowed and e.type not in allowed:
            continue
        if q and q.lower() not in e.name.lower() and not any(q.lower() in a.lower() for a in e.aliases):
            continue
        entities.append({
            "name": e.name, "type": e.type, "description": e.description,
            "degree": e.degree, "community_id": e.community_id, "aliases": e.aliases,
        })
    return ok_envelope(entities[:200], trace_id, elapsed_ms_since(started))


@router.get("/graph/entity/{name}", response_model=Envelope)
def entity_detail(name: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = _graph_store(request)
    # 别名归一查找
    canonical = request.app.state.entity_resolver.lookup(name) or name
    entity = store.get_entity(canonical)
    if entity is None:
        raise ApiError(ErrCode.ENTITY_NOT_FOUND, f"实体未在图谱中: {name}", http_status=404)
    community = next(
        (c for c in store.communities() if c.community_id == entity.community_id), None
    )
    neighbors = store.neighbors(entity.name, depth=1)
    in_edges = [
        {"from": p["from"], "relation": p["relation"], "description": p["description"]}
        for p in neighbors if p["direction"] == "backward"
    ]
    out_edges = [
        {"to": p["to"], "relation": p["relation"], "description": p["description"]}
        for p in neighbors if p["direction"] == "forward"
    ]
    return ok_envelope({
        "name": entity.name, "type": entity.type, "description": entity.description,
        "aliases": entity.aliases, "degree": entity.degree,
        "community": {
            "community_id": community.community_id,
            "summary": community.summary,
            "size": community.size,
        } if community else None,
        "in_edges": in_edges, "out_edges": out_edges,
    }, trace_id, elapsed_ms_since(started))


@router.get("/graph/subgraph", response_model=Envelope)
def subgraph(
    request: Request,
    seed: str = Query(default=""),
    depth: int = Query(default=1, ge=1, le=3),
) -> Envelope:
    """子图（画布渲染）。默认展示高连接度核心实体（不用空白画布面对用户）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = _graph_store(request)

    nodes: dict[str, dict] = {}
    links: list[dict] = []
    truncated = False

    def add_entity(name: str) -> None:
        e = store.get_entity(name)
        if e and name not in nodes:
            nodes[name] = {"id": name, "type": e.type, "degree": e.degree}

    if seed:
        canonical = request.app.state.entity_resolver.lookup(seed) or seed
        for item in store.neighbors(canonical, depth=depth):
            if len(links) >= SUBGRAPH_MAX_EDGES:
                truncated = True
                break
            add_entity(item["from"])
            add_entity(item["to"])
            links.append({
                "source": item["from"], "target": item["to"],
                "relation": item["relation"], "direction": "forward",
                "weight": item["weight"],
            })
    else:
        # 默认视图：高连接度核心实体的一度邻居
        core = store.all_entities()[:10]
        for entity in core:
            for item in store.neighbors(entity.name, depth=1):
                if len(links) >= SUBGRAPH_MAX_EDGES:
                    truncated = True
                    break
                add_entity(item["from"])
                add_entity(item["to"])
                links.append({
                    "source": item["from"], "target": item["to"],
                    "relation": item["relation"], "direction": "forward",
                    "weight": item["weight"],
                })

    return ok_envelope({
        "nodes": list(nodes.values()),
        "links": links,
        "truncated": truncated,
    }, trace_id, elapsed_ms_since(started))


@router.get("/graph/communities", response_model=Envelope)
def list_communities(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = _graph_store(request)
    return ok_envelope([
        {
            "community_id": c.community_id, "size": c.size,
            "summary": c.summary, "members": c.members[:10],
            "generated_at": c.generated_at, "model_version": c.model_version,
        }
        for c in store.communities()
    ], trace_id, elapsed_ms_since(started))


class BuildGraphRequest(BaseModel):
    doc_id: str | None = None  # 指定文档重建；空 = 全量
    rebuild_communities: bool = True


@router.post("/graph/build", response_model=Envelope)
def build_graph(req: BuildGraphRequest, request: Request) -> Envelope:
    """从已有块构建图谱（显式触发——抽取成本是 GraphRAG 主要成本，不随上传自动跑）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    builder = request.app.state.graph_builder
    store = request.app.state.store
    chunks = store.all_chunks()
    if req.doc_id:
        chunks = [c for c in chunks if c.doc_id == req.doc_id]
        if not chunks:
            raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, f"文档无块: {req.doc_id}", http_status=404)
    result = builder.build_from_chunks(chunks, rebuild_doc_id=req.doc_id)
    if req.rebuild_communities:
        result["community"] = builder.rebuild_communities()
    return ok_envelope(result, trace_id, elapsed_ms_since(started))


@router.get("/graph/stats", response_model=Envelope)
def graph_stats(request: Request) -> Envelope:
    """图谱健康度（孤立实体占比 > 20% 说明关系抽取质量下降）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    stats = _graph_store(request).stats()
    stats["images"] = request.app.state.image_index.stats()["images"]
    stats["caption_model"] = request.app.state.image_index.stats()["model_version"]
    return ok_envelope(stats, trace_id, elapsed_ms_since(started))


class MediaSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=4, ge=1, le=10)


@router.post("/search/media", response_model=Envelope)
def search_media(req: MediaSearchRequest, request: Request) -> Envelope:
    """以文搜图（图像描述向量与正文同空间）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant = request.headers.get("X-Tenant-Id", "default")
    items = request.app.state.image_index.search(req.query, req.top_k, tenant)
    return ok_envelope({"items": items, "total": len(items)}, trace_id, elapsed_ms_since(started))


class IndexImagesRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)  # 单次索引上限（VL 调用成本护栏）


@router.post("/media/index", response_model=Envelope)
def index_images(req: IndexImagesRequest, request: Request) -> Envelope:
    """扫描资源目录，为未建索引的图像生成描述并入统一向量库（显式触发，成本可控）。"""
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    settings = request.app.state.settings
    assets_dir = settings.data_dir / "assets"
    image_index = request.app.state.image_index
    indexed, failed, skipped = 0, 0, 0
    if assets_dir.exists():
        for img in sorted(assets_dir.glob("*.png"))[: req.limit]:
            asset_id = img.stem
            if asset_id in image_index._entries:
                skipped += 1
                continue
            # 从 "doc#page-N" 形态的资产名解析来源页码（溯源字段）
            page = 0
            if "#page-" in asset_id:
                try:
                    page = int(asset_id.rsplit("#page-", 1)[1])
                except ValueError:
                    page = 0
            entry = image_index.index_image(
                img, asset_id, doc_id=asset_id.split("#")[0], page=page,
                tenant_id=settings.default_tenant_id,
            )
            if entry:
                indexed += 1
            else:
                failed += 1
    return ok_envelope(
        {"indexed": indexed, "failed": failed, "skipped": skipped},
        trace_id, elapsed_ms_since(started),
    )


@router.get("/assets/{name}")
def serve_asset(name: str, request: Request) -> FileResponse:
    """图像资源服务（路径安全：只允许 assets 目录内的文件）。"""
    assets_dir: Path = request.app.state.settings.data_dir / "assets"
    path = (assets_dir / name).resolve()
    if not str(path).startswith(str(assets_dir.resolve())) or not path.exists():
        raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, "资源不存在", http_status=404)
    return FileResponse(path)
