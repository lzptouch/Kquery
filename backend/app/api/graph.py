"""图谱与多模态检索端点。

- GET /graph/entities：实体列表（搜索 + 类型过滤）；
- GET /graph/entity/{name}：实体详情（入边/出边带方向）；
- GET /graph/subgraph：子图（画布渲染，truncated 截断提示）；
- GET /graph/communities：社区列表与摘要；
- POST /graph/build：从已有块构建/重建图谱（显式触发，成本可控）；
- GET /graph/stats：图谱健康度指标（孤立实体占比等）；
- POST /search/media：以文搜图；
- GET /assets/{name}：图像资源服务（前端渲染用，路径安全校验）。
"""
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, get_trace_id, now_ms, set_trace_id
from app.dependencies import enforce_rate_limit
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["graph"])
SUBGRAPH_MAX_EDGES = 100  # 子图截断上限（超出必须提示）


def _graph_store(request: Request):
    return request.app.state.graph_store


def _tenant(request: Request) -> str:
    """当前租户（图谱数据按租户分区，不得跨租户可见）。"""
    return request.state.principal.tenant_id


@router.get("/graph/entities", response_model=Envelope)
def list_entities(
    request: Request,
    q: str = Query(default="", max_length=100),
    types: str = Query(default=""),
) -> Envelope:
    """图谱实体列表（按度数 / 名称过滤，分页）。

    图谱不可用时返回空列表而不是 404（图谱是增强能力，不是主链路）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = _graph_store(request)
    allowed = {t.strip() for t in types.split(",") if t.strip()}
    entities = []
    for e in store.all_entities(_tenant(request)):
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
    """单个实体的详情：邻接关系、社区归属、出现过的文档。

    实体不存在 → 404（`ENTITY_NOT_FOUND`）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = _graph_store(request)
    # 别名归一查找（按本租户分区，KQ-06）
    canonical = request.app.state.entity_resolver.lookup(name, tenant_id=_tenant(request)) or name
    entity = store.get_entity(canonical, _tenant(request))
    if entity is None:
        raise ApiError(ErrCode.ENTITY_NOT_FOUND, f"实体未在图谱中: {name}", http_status=404)
    community = next(
        (c for c in store.communities(_tenant(request)) if c.community_id == entity.community_id), None
    )
    neighbors = store.neighbors(entity.name, depth=1, tenant_id=_tenant(request))
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
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = _graph_store(request)

    nodes: dict[str, dict] = {}
    links: list[dict] = []
    truncated = False

    def add_entity(name: str) -> None:
        e = store.get_entity(name, _tenant(request))
        if e and name not in nodes:
            nodes[name] = {"id": name, "type": e.type, "degree": e.degree}

    if seed:
        canonical = request.app.state.entity_resolver.lookup(seed, tenant_id=_tenant(request)) or seed
        for item in store.neighbors(canonical, depth=depth, tenant_id=_tenant(request)):
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
        core = store.all_entities(_tenant(request))[:10]
        for entity in core:
            for item in store.neighbors(entity.name, depth=1, tenant_id=_tenant(request)):
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
    """社区（社区摘要）列表：层级、大小、代表实体。

    社区数由 `community_max` 限制，超出后按权重截断。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    store = _graph_store(request)
    return ok_envelope([
        {
            "community_id": c.community_id, "size": c.size,
            "summary": c.summary, "members": c.members[:10],
            "generated_at": c.generated_at, "model_version": c.model_version,
        }
        for c in store.communities(_tenant(request))
    ], trace_id, elapsed_ms_since(started))


class BuildGraphRequest(BaseModel):
    doc_id: str | None = None  # 指定文档重建；空 = 全量
    rebuild_communities: bool = True


def _run_graph_build(app, task) -> dict:
    """在 worker 线程里真正干活（`C-09`）。

    取块放在后台而不是接口层：`all_chunks()` 在大库上本身就要几百毫秒到几秒，
    留在请求线程等于「后台化只做了一半」。
    """
    builder = app.state.graph_builder
    store = app.state.store
    chunks = store.all_chunks()
    if task.doc_id:
        chunks = [c for c in chunks if c.doc_id == task.doc_id]
        if not chunks:
            raise ValueError(f"文档无块: {task.doc_id}")
    else:
        # 全量构建：只建本租户的块（跨租户全量构建会把其它租户的图改写掉）
        chunks = [c for c in chunks if c.tenant_id == task.tenant_id]
    task.chunk_count = len(chunks)
    task.stage = "extracting"
    result = builder.build_from_chunks(
        chunks, rebuild_doc_id=task.doc_id, tenant_id=task.tenant_id
    )
    if task.rebuild_communities:
        task.stage = "communities"
        result["community"] = builder.rebuild_communities(tenant_id=task.tenant_id)
    return result


@router.post("/graph/build", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def build_graph(req: BuildGraphRequest, request: Request) -> Envelope:
    """从已有块构建图谱：**受理即返回**，进度另查（`C-09`）。

    同步版本在真实库上是分钟级操作，而 HTTP 超时通常 30~60 秒：
    客户端超时、后台其实跑完了，用户重试一次就再跑一遍。现在返回 `task_id`，
    由 `GET /graph/build/{task_id}` 查状态。
    """
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant = _tenant(request)

    # 租户断言必须同步做（快且是权限边界）：不能等到后台线程里才发现越权
    if req.doc_id:
        doc = request.app.state.knowledge.get(req.doc_id)
        if doc.tenant_id != tenant:
            raise ApiError(ErrCode.DOCUMENT_NOT_FOUND, f"文档不存在: {req.doc_id}", http_status=404)

    registry = request.app.state.graph_build_tasks
    try:
        task = registry.submit(
            lambda t: _run_graph_build(request.app, t),
            tenant_id=tenant,
            doc_id=req.doc_id,
            rebuild_communities=req.rebuild_communities,
        )
    except RuntimeError as exc:
        # 排队满 → 明确拒绝（拿到一个永远 queued 的 task_id 比拒绝更糟）
        raise ApiError(
            ErrCode.RATE_LIMITED, str(exc), http_status=503, retry_after=10
        ) from exc

    return ok_envelope(
        {"task_id": task.task_id, "status": task.status, "doc_id": task.doc_id},
        trace_id,
        elapsed_ms_since(started),
    )


@router.get("/graph/build/{task_id}", response_model=Envelope)
def get_build_task(task_id: str, request: Request) -> Envelope:
    """图谱构建任务状态（只对本租户可见）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    task = request.app.state.graph_build_tasks.get(task_id)
    if task is None or task.tenant_id != _tenant(request):
        raise ApiError(ErrCode.TASK_NOT_FOUND, f"任务不存在: {task_id}", http_status=404)
    return ok_envelope(task.snapshot(), trace_id, elapsed_ms_since(started))


@router.get("/graph/stats", response_model=Envelope)
def graph_stats(request: Request) -> Envelope:
    """图谱健康度（孤立实体占比 > 20% 说明关系抽取质量下降）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    stats = _graph_store(request).stats(_tenant(request))
    stats["images"] = request.app.state.image_index.stats()["images"]
    stats["caption_model"] = request.app.state.image_index.stats()["model_version"]
    return ok_envelope(stats, trace_id, elapsed_ms_since(started))


class MediaSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=4, ge=1, le=10)


@router.post("/search/media", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def search_media(req: MediaSearchRequest, request: Request) -> Envelope:
    """以文搜图（图像描述向量与正文同空间）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    tenant = request.state.principal.tenant_id
    items = request.app.state.image_index.search(req.query, req.top_k, tenant)
    return ok_envelope({"items": items, "total": len(items)}, trace_id, elapsed_ms_since(started))


class IndexImagesRequest(BaseModel):
    limit: int = Field(default=10, ge=1, le=50)  # 单次索引上限（VL 调用成本护栏）
    # `LM-02`：一次提交多张（逐张结果独立返回，部分失败不回滚整批）。
    # 上限 50 是**成本护栏**（VL 调用很贵），与「一次传多少张」是同一个闸门。
    asset_ids: list[str] = Field(default_factory=list, max_length=50)


def _build_batch_items(
    paths: list[Path],
    image_index,
    knowledge,
    caller_tenant: str,
) -> list[tuple[Path, str, str, int, str]]:
    """把资产路径转成批量索引的入参（沿用单张路径的租户 / 页码解析口径）。"""
    items: list[tuple[Path, str, str, int, str]] = []
    for img in paths:
        asset_id = img.stem
        if asset_id in image_index._entries:
            continue
        page = 0
        if "#page-" in asset_id:
            try:
                page = int(asset_id.rsplit("#page-", 1)[1])
            except ValueError:
                page = 0
        doc_id = asset_id.split("#")[0]
        try:
            doc_tenant = knowledge.get(doc_id).tenant_id
        except Exception:  # noqa: BLE001 - 无主资产回落到调用主体租户
            doc_tenant = caller_tenant
        if doc_tenant != caller_tenant:
            continue  # 不替其它租户建索引
        items.append((img, asset_id, doc_id, page, doc_tenant))
    return items

@router.post("/media/index", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
def index_images(req: IndexImagesRequest, request: Request) -> Envelope:
    """扫描资源目录，为未建索引的图像生成描述并入统一向量库（显式触发，成本可控）。"""
    trace_id = get_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    settings = request.app.state.settings
    assets_dir = settings.data_dir / "assets"
    image_index = request.app.state.image_index
    knowledge = request.app.state.knowledge
    caller_tenant = _tenant(request)
    indexed, failed, skipped = 0, 0, 0
    # `LM-02`：指定了 asset_ids → 走批量路径（逐张并发，逐张成败独立）
    if req.asset_ids:
        targets: list[Path] = []
        for asset_id in req.asset_ids:
            path = assets_dir / f"{asset_id}.png"
            if path.exists():
                targets.append(path)
        batch = image_index.index_images_batch(
            _build_batch_items(targets, image_index, knowledge, caller_tenant),
            concurrency=max(1, min(8, len(targets) or 1)),
        )
        for row in batch:
            if row.get("ok"):
                indexed += 1
            else:
                failed += 1
        return ok_envelope(
            {
                "indexed": indexed,
                "failed": failed,
                "skipped": skipped,
                "results": batch,
            },
            trace_id, elapsed_ms_since(started),
        )

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
            doc_id = asset_id.split("#")[0]
            # KQ-02：租户取来源文档的租户，而不是一律 default
            try:
                doc_tenant = knowledge.get(doc_id).tenant_id
            except Exception:  # noqa: BLE001 - 无主资产回落到调用主体租户
                logger.debug("无主资产回落到调用主体租户")
                doc_tenant = caller_tenant
            if doc_tenant != caller_tenant:
                skipped += 1  # 不替其它租户建索引（也不报错，避免泄露存在性）
                continue
            entry = image_index.index_image(
                img, asset_id, doc_id=doc_id, page=page,
                tenant_id=doc_tenant,
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
    # is_relative_to：startswith 前缀比较会被同名前缀的兄弟目录绕过（assets2/...）
    if not path.is_relative_to(assets_dir.resolve()) or not path.exists():
        # 素材（图片/媒体）缺失与「文档缺失」是两件事：前端对素材走**降级占位**
        # （整卡不失败），对文档缺失才是错误页。用不同错误码，前端才能区分。
        raise ApiError(ErrCode.ASSET_NOT_FOUND, "资源不存在", http_status=404)
    # KQ-02：资产归属来源文档的租户（资产名形如 "doc#page-N"）
    doc_id = path.stem.split("#", 1)[0]
    if doc_id:
        try:
            doc = request.app.state.knowledge.get(doc_id)
            if doc.tenant_id != _tenant(request):
                raise ApiError(ErrCode.ASSET_NOT_FOUND, "资源不存在", http_status=404)
        except ApiError:
            raise
        except Exception:  # noqa: BLE001 - 无主资产不放行（默认拒绝）
            raise ApiError(ErrCode.ASSET_NOT_FOUND, "资源不存在", http_status=404) from None
    return FileResponse(path)
