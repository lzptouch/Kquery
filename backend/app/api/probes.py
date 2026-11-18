"""探针与运营指标端点（`/health` · `/livez` · `/readyz` · `/healthz` · `/metrics`）。

## 为什么单独成 router

这五个端点是**运维契约**，与业务端点无关：它们的调用方是 K8s kubelet 与
Prometheus，不是前端。放在 `api/` 里独立成文件，既满足「一个 HTTP 端点 →
`api/<域>.py`」的落位规则，也让 `bootstrap.py` 保持「只注册、不实现」。

## 语义边界（三条，改写前先读）

- `/livez` **绝不查依赖**：依赖抖动会误杀 Pod；
- `/readyz` 把「索引为空」与「未就绪」区分开：空索引可服务，否则新部署永远进不来；
- `/metrics` 默认要鉴权：指标里含路径、状态码、工具名与时延分布，属于运营数据。
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.core.envelope import ApiError, ErrCode
from app.core.logging_setup import get_logger
from app.core.probes import (
    METRICS_OPEN_ENVS,
    PROBE_STATUS_HTTP,
    alias_version,
    env_of,
    probe_all,
)
from app.services.upstream import bulkheads_stats

logger = get_logger(__name__)

router = APIRouter()


@router.get("/health")
def health() -> dict:
    """极简存活检查：不查依赖、不碰存储，只证明进程能响应请求。"""
    return {"status": "up"}


@router.get("/livez")
def livez() -> PlainTextResponse:
    """存活探针：只证明进程在跑，**绝不查依赖**（依赖抖动会误杀 Pod）。"""
    return PlainTextResponse("ok", media_type="text/plain; charset=utf-8")


@router.get("/readyz")
def readyz(request: Request, response: Response) -> JSONResponse:
    """就绪探针：判断能否接流量。失败 → 摘流，不抛错（由 K8s 读状态码）。

    语义边界：**"索引为空"不等于"未就绪"**。全新部署没有任何文档时索引
    本来就是 0，若据此返回 503，Service 会永远没有 endpoints，
    请求根本进不来，也就永远没人能上传第一份文档——死锁。
    真正该摘流的是"依赖不可达/集合没加载起来"，那种情况 store 为 None。
    """
    store = getattr(request.app.state, "store", None)
    total = int(getattr(store, "total", 0) or 0)
    ready = store is not None
    # 共享状态（Redis）不可达 → 限流额度按副本数放大、缓存跨副本失效：
    # 这是"能接流量但语义已变"的状态，必须摘流（X-18）。
    shared_ok = True
    shared_detail: dict = {}
    try:
        shared = getattr(request.app.state, "shared_state", None)
        if shared is not None:
            # backend 取值：redis / local / local(redis-degraded)。
            # 只有"Redis 配了但连不上"才算不健康——那时限流额度按副本数放大、
            # 缓存跨副本失效，属于"能接流量但语义已变"，必须摘流。
            # 压根没配 Redis（local）是**单副本语义**的已知状态，不摘流。
            backend = shared.backend
            shared_detail = {"shared_state": backend}
            shared_ok = backend != "local(redis-degraded)"
    except Exception:  # noqa: BLE001 - 探针不该因取状态失败而 500
        shared_ok = False
    ready = ready and shared_ok
    body = {
        "status": "ready" if ready else "not_ready",
        "index_chunks": total,
        # 空索引单独标注：可服务但答不出东西，与"未就绪"是两回事，
        # 值班同学看这一行就知道该不该去查数据而不是查服务
        "empty_index": ready and total == 0,
        **shared_detail,
    }
    if ready and total == 0:
        logger.warning("就绪但索引为空：服务可接流量，检索会返回空结果")
    return JSONResponse(
        status_code=200 if ready else 503,
        content=body,
    )


@router.get("/healthz")
def healthz(request: Request) -> JSONResponse:
    """完整健康报告：逐个依赖的状态、耗时、错误摘要。不参与流量路由。

    **并行探测 + 单项超时**（X-18）：此前 9 个依赖逐个同步调用，
    任一依赖慢 5s → 整个探针 5s，K8s 的 probe timeout 会误杀 Pod；
    且串行耗时是各项之和。并行后总耗时 = 最慢的那一项，
    再加单项上限，探针耗时**有界**。

    **错误不回显原文**：`str(exc)` 可能含连接串（redis://:pwd@host）、
    内部路径与库表名。只回**异常类型** + 统一错误码：
    排障靠 trace_id 反查日志，不需要把秘密写进探针响应。

    **带错误码**（`A-05`）：每个 down 的依赖都带 `code`（`ErrCode` 52xx 段，
    见 `PROBE_ERROR_CODES`），调用方按码判定「哪一类依赖坏了」并差异化处置；
    顶层 `code` 非 0 表示「有依赖不可用」。状态 ↔ HTTP 映射见 `PROBE_STATUS_HTTP`
    （up=200 / down=503）。
    """
    state = request.app.state
    settings = state.settings

    probes: list[tuple[str, object]] = [
        ("vector_store", lambda: {"chunks": int(getattr(state.store, "total", 0) or 0)}),
        ("bm25_index", lambda: {"chunks": int(getattr(state.bm25, "total", 0) or 0)}),
        ("graph_store", lambda: state.graph_store.stats()),
        # /117：探针暴露当前后端，避免"以为在用 Redis 其实已降级"
        ("shared_state", lambda: state.shared_state.snapshot()),
        ("long_term_memory", lambda: state.long_term_memory.stats()
         if state.long_term_memory else {"enabled": False}),
        ("parser_router", lambda: {"supported": sorted(state.parser_router.supported_extensions())}),
        ("provider", lambda: {"provider": settings.resolved_provider, "llm": settings.llm_model}),
        # 认证档位进健康报告：HeaderAuthProvider 是开发档，运维一眼就能看到（KQ-03）
        # 批次 2：上游水位（许可占用/熔断状态）进健康报告
        ("upstream", bulkheads_stats),
    ]
    deps = probe_all(probes, timeout_s=settings.health_probe_timeout_s)
    deps.sort(key=lambda d: d["name"])  # 稳定顺序：并行完成顺序不确定

    down = [d["name"] for d in deps if d["status"] == "down"]
    return JSONResponse(
        status_code=PROBE_STATUS_HTTP["up"] if not down else PROBE_STATUS_HTTP["down"],
        content={
            "status": "healthy" if not down else "degraded",
            # 顶层错误码：0 = 全部正常；非 0 = 有依赖不可用（分项码在 dependencies 里）
            "code": 0 if not down else int(ErrCode.DEPENDENCY_UNAVAILABLE),
            "down": down,
            "dependencies": deps,
            "data_version": int(getattr(state.knowledge, "data_version", 0) or 0),
            # 索引别名版本：切换物理索引后自增 → 前端据此失效缓存
            "index_alias_version": alias_version(state),
        },
    )


@router.get("/metrics")
def metrics(request: Request) -> PlainTextResponse:
    """Prometheus 抓取端点（运营数据，不进用户响应）。

    **默认需要鉴权**（X-18）：此前匿名可抓 —— 指标里含路径、状态码、
    工具名与时延分布，属于运营数据；匿名可抓等于把它发到公网。
    Prometheus 用 `METRICS_TOKEN` 拉取；显式设 `metrics_public=true`
    才对内网开放（此时仍**不**进入用户响应链路）。
    """
    settings = request.app.state.settings
    if not bool(getattr(settings, "metrics_public", False)):
        token = str(getattr(settings, "metrics_token", "") or "")
        provided = (request.headers.get("X-Metrics-Token") or "").strip()
        if token:
            # 配了令牌 → 必须带对（Prometheus 侧用 X-Metrics-Token 头）
            if provided != token:
                raise ApiError(
                    ErrCode.UNAUTHORIZED, "metrics 需要鉴权（X-Metrics-Token）", http_status=401
                )
        elif env_of(settings) not in METRICS_OPEN_ENVS:
            # 非开发档却**没配令牌** → fail-closed：
            # 宁可 401 也不把运营数据裸露出去（fail-open 等于默认不安全）。
            #
            # 这里原先读 `settings.auth_enabled`——该配置项已随认证下线删除，
            # `getattr(..., False)` 只能拿到恒假的默认值，于是这条 fail-closed
            # **从未生效**（生产漏配令牌照样匿名可抓）。改用 `app_env` 判定后
            # 才真正按环境区分：只有 dev/local/test 允许无令牌裸抓。
            raise ApiError(
                ErrCode.UNAUTHORIZED,
                "metrics 未配置 METRICS_TOKEN（生产环境禁止匿名抓取）",
                http_status=401,
            )
        # 开发档（app_env ∈ dev/local/test）且未配令牌 → 放行（本地调试友好）
    return PlainTextResponse(
        request.app.state.prom.render(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
