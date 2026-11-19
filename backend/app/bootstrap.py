"""应用外壳（`A-12`）：FastAPI 实例 / 中间件 / 异常处理器 / 路由注册。

## 本模块回答什么、不回答什么

- **回答**「应用长什么样」：有哪些路由、错误怎么翻译成信封、每个请求怎么带 trace_id；
- **不回答**「对象怎么建」：装配顺序与依赖关系在 `app/assembly/lifespan.py`。

因此本模块可以被**独立导入**（不触发任何装配），这也是 `A-12` 的验收条件之一：
改一条路由不该把整条问答链路拉起来。

## 五条纪律（改动前先读）

1. **错误指标只在异常处理器里计数**（`A-09`）：抛错点分散在各层，漏计与重复计数都是必然的；
2. **一次异常只在一处记堆栈**：`DomainError` 在处理器里记，端点不记；
3. **404 分层**：路由未命中（路径问题）与资源不存在（业务问题）分开编码；
4. **路由 label 用模板**（`/knowledge/{doc_id}`），原始路径会把 doc_id
   变成高基数 label；
5. **生产关闭交互式文档**：`/docs`、`/redoc`、`/openapi.json` 按 `app_env` 关，
   一刀切会把依赖 OpenAPI 的端点守卫一起打瞎。
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import admin as admin_api
from app.api import analysis as analysis_api
from app.api import ask as ask_api
from app.api import feedback as feedback_api
from app.api import graph as graph_api
from app.api import ingest as ingest_api
from app.api import knowledge as knowledge_api
from app.api import probes as probes_api
from app.api import quickask as quickask_api
from app.api import sessions as sessions_api
from app.assembly.lifespan import lifespan
from app.core.config import get_settings
from app.core.envelope import ApiError, ErrCode, error_envelope, validate_code_uniqueness
from app.core.errors import DomainError
from app.core.identity import Principal
from app.core.logging_setup import get_logger, setup_logging
from app.core.otel import setup_otel
from app.core.probes import env_of
from app.core.tracing import (
    elapsed_ms_since,
    get_trace_id,
    new_trace_id,
    now_ms,
    reset_request_context,
    reset_trace_id,
    set_request_context,
    set_trace_id,
)

logger = get_logger(__name__)


def _app_version() -> str:
    """应用版本号：以 pyproject.toml 为唯一来源，未安装为包时兜底。"""
    try:
        from importlib.metadata import version

        return version("kquery-backend")
    except Exception:  # noqa: BLE001 - 含 PackageNotFoundError 与元数据缺失
        return "0.1.0"


def _record_error(request: Request, code: int) -> None:
    """错误指标计数（`A-09`）的**唯一入口**。

    为什么放在异常处理器而不是抛错点：
    - 抛错点分散在 service / api / core 各层，漏计是必然的；
    - 同一个错误可能在多层各抛一次（包装后重抛），在抛错点计数会重复计数；
    - 异常处理器是「错误响应真正产生」的地方，**每个错误响应恰好经过一次**。

    指标采集失败不得影响请求（与 `PromMetrics.render` 同纪律）。
    """
    try:
        prom = getattr(request.app.state, "prom", None)
        if prom is not None:
            prom.record_error(int(code))
    except Exception:  # noqa: BLE001 - 指标失败不该影响错误响应
        pass


def install_error_handlers(app: FastAPI) -> None:
    """注册五级异常处理器：领域异常 → 业务异常 → 校验 → HTTP → 兜底。

    **注册顺序即优先级说明**：Starlette 按异常 MRO 精确匹配，
    `DomainError` 是 `ApiError` 的子类，因此领域异常只会走自己的处理器。
    """

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        """领域异常：按「调用方该怎么做」给出状态码与错误码（`A-07`）。

        **一次异常只在一处记堆栈**：这里记（`exc_info=exc`），端点不记——
        两处都记会得到两份堆栈，排障时反而分不清是哪条路径。
        """
        trace_id = get_trace_id()
        logger.error(
            "领域异常 code=%s path=%s trace_id=%s: %s",
            int(exc.code), request.url.path, trace_id, exc.message,
            exc_info=exc,
        )
        envelope = error_envelope(exc.code, exc.message, trace_id, 0)
        _record_error(request, exc.code)
        return JSONResponse(status_code=exc.http_status, content=envelope.model_dump())

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        """业务异常：统一信封 + 可选 `Retry-After`。"""
        # 复用请求入口的 trace_id：用户从前端复制的追踪号必须能反查到本次请求日志
        trace_id = get_trace_id()
        envelope = error_envelope(exc.code, exc.message, trace_id, 0)
        headers = {}
        if exc.retry_after is not None:
            headers["Retry-After"] = str(exc.retry_after)
        # 错误指标（`A-09`）：**在异常处理器里计数**，不在抛错点计数——
        # 抛错点分散在各层，漏计与重复计数都是必然的。
        _record_error(request, exc.code)
        return JSONResponse(
            status_code=exc.http_status, content=envelope.model_dump(), headers=headers
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """把 FastAPI 的校验错误翻译成统一信封。

        不加这个处理器时，参数缺失会返回 FastAPI 默认的 `{"detail": [...]}`，
        与其余端点的 `{"status","data","error"}` 结构不一致。后果很实际：
        前端 `request()` 统一读 `body.error`，遇到这种响应会拿到 undefined，
        于是页面上显示成 "undefined" 或者干脆空白——而用户填错一个字段
        本该看到"缺少 question"这种能照着改的提示。
        """
        trace_id = get_trace_id()
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        field = loc.split(".")[-1] if loc else "请求"
        message = f"{field} {first.get('msg', '参数不合法')}".strip()
        logger.info(
            "请求参数校验失败 trace_id=%s path=%s detail=%s",
            trace_id, request.url.path, first.get("msg"),
        )
        envelope = error_envelope(ErrCode.INVALID_INPUT, message, trace_id, 0)
        _record_error(request, ErrCode.INVALID_INPUT)
        return JSONResponse(status_code=400, content=envelope.model_dump())

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """404/405 等也走统一信封，否则前端同样读不到 error.message。

        **404 分层**（X-04）：此前任何 404 都报"文档不存在"——
        URL 拼错（路由未命中）被翻译成业务错误，调用方会去查文档而不是查路径。
        现在按"有没有命中路由"区分：
        - 路由未命中 → `INVALID_REQUEST`（1004，路径问题）
        - 路由命中但资源不存在 → `DOCUMENT_NOT_FOUND`（3002，业务问题）
        """
        trace_id = get_trace_id()
        if exc.status_code == 404:
            route = request.scope.get("route")
            # scope["route"] 为 None 说明**没有任何路由匹配**这个路径
            code = (
                ErrCode.DOCUMENT_NOT_FOUND
                if route is not None
                else ErrCode.INVALID_REQUEST
            )
        else:
            code = {
                405: ErrCode.INVALID_REQUEST,
                413: ErrCode.FILE_TOO_LARGE,
            }.get(exc.status_code, ErrCode.INVALID_REQUEST)
        envelope = error_envelope(code, str(exc.detail), trace_id, 0)
        _record_error(request, code)
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(),
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        """兜底：不把内部异常原文曝给调用方，只回 trace_id。"""
        trace_id = get_trace_id()
        logger.exception("未处理异常 trace_id=%s", trace_id)
        envelope = error_envelope(
            ErrCode.INTERNAL_ERROR, "服务暂时不可用，请稍后再试", trace_id, 0
        )
        _record_error(request, ErrCode.INTERNAL_ERROR)
        return JSONResponse(status_code=500, content=envelope.model_dump())


def install_trace_middleware(app: FastAPI) -> None:
    """注册 trace_id 与请求级上下文的中间件。"""

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        """每个请求一个 trace_id（入站优先复用网关传来的，便于跨服务串联）。

        同时把**请求级上下文**（租户/用户/路由/耗时）写进 contextvar：
        这是日志字段的**唯一生产者**（X-15）——此前这四个字段"结构对了、内容空"，
        因为没有任何地方往日志记录里写它们。业务代码不必各自拼字段。
        """
        incoming = request.headers.get("X-Trace-Id", "").strip() or new_trace_id()
        token = set_trace_id(incoming)
        request.state.trace_id = incoming
        request.state.started_ms = None
        # 请求身份（固定值）：认证已下线后**每个**请求都有身份，
        # 不再依赖某个 router 恰好挂了 require_role/get_principal 才会被设置。
        # 此前 `request.state.principal` 是"谁挂了 get_principal 谁才有"，
        # 角色门禁下线后 graph 等只读端点就会 500（AttributeError: 'principal'）。
        request.state.principal = Principal()
        # 路由用**模板**（/knowledge/{doc_id}）而不是原始路径：
        # 原始路径会把 doc_id 变成高基数 label，日志与指标都会被撑爆。
        route = request.scope.get("route")
        route_path = getattr(route, "path", "") or request.url.path
        # 租户/用户取**固定身份**而非请求头：头里的 X-Tenant-Id / X-User-Id
        # 已无权改变主体，写进日志只会让日志记录被请求方控制的字段。
        ctx_token = set_request_context(
            trace_id=incoming,
            tenant_id=request.state.principal.tenant_id,
            user_id=request.state.principal.user_id,
            route=route_path,
        )
        started = now_ms()
        try:
            response = await call_next(request)
        finally:
            response_trace = getattr(request.state, "trace_id", incoming)
            reset_trace_id(token)
        elapsed = elapsed_ms_since(started)
        # 耗时回填：请求结束后才知道，必须再写一次上下文
        set_request_context(latency_ms=elapsed)
        try:
            reset_request_context(ctx_token)
        except Exception:  # noqa: BLE001 - 请求上下文重置失败：不影响响应
            pass
        response.headers["X-Trace-Id"] = response_trace
        # HTTP 访问指标（X-16）：route 用**模板**——此时路由已匹配，
        # scope["route"].path 形如 /knowledge/{doc_id}；用原始路径会造出
        # 每文档一条时间序列（高基数反模式）。
        #
        # 未匹配路由（404/扫描器乱打）**必须**收敛到固定占位符 `C-05`：
        # 此前 404 走 `route_path` 回退到 `request.url.path`，等于让任意请求方
        # 决定 label 取值——`/wp-login.php`、随机 UUID 每次都会新建一条时间序列，
        # Prometheus 侧就是无界增长。日志里的 route 仍保留原始路径（可检索），
        # 但**指标**只认路由表。
        METRIC_UNMATCHED_ROUTE = "<unmatched>"
        prom = getattr(request.app.state, "prom", None)
        if prom is not None:
            try:
                matched = request.scope.get("route")
                template = getattr(matched, "path", "") or METRIC_UNMATCHED_ROUTE
                prom.http_requests_total.labels(
                    method=request.method, route=template, status=str(response.status_code)
                ).inc()
                prom.http_latency_seconds.labels(
                    method=request.method, route=template
                ).observe(elapsed / 1000)
            except Exception:  # noqa: BLE001 - 埋点失败不该影响请求
                pass
        return response


def register_routes(app: FastAPI) -> None:
    """注册全部路由：探针在前（运维契约），业务端点在后。"""
    app.include_router(probes_api.router)
    app.include_router(ask_api.router)
    app.include_router(knowledge_api.router)
    app.include_router(ingest_api.router)
    app.include_router(analysis_api.router)
    app.include_router(feedback_api.router)
    app.include_router(admin_api.router)
    app.include_router(sessions_api.router)
    app.include_router(graph_api.router)
    app.include_router(quickask_api.router)


def create_app() -> FastAPI:
    """建应用外壳：校验 → 日志/追踪 → 中间件 → 异常处理器 → 路由。"""
    # 错误码编号必须全局唯一（IntEnum 重复值会产生别名，按码分流失效）
    validate_code_uniqueness()
    settings = get_settings()
    # 结构化日志。文本形态保留给本地开发（看 JSON 太痛苦），
    # 生产设 LOG_FORMAT=json 后逐行 JSON + DEBUG 采样 + PII 脱敏。
    setup_logging(
        level=getattr(settings, "log_level", "INFO"),
        log_format=getattr(settings, "log_format", "text"),
        debug_sample_rate=float(getattr(settings, "log_debug_sample_rate", 0.1)),
    )

    # OTel（未启用/未配置 endpoint 时静默降级为 NoOp）
    setup_otel(settings)

    # 启动期配置校验：缺项直接拒绝启动，而不是让故障延后到运行期
    issues = settings.validate_startup()
    if issues:
        detail = "\n".join(
            f"  - {i['field']}: {i['reason']}（{i['hint']}）" for i in issues
        )
        raise RuntimeError(f"启动期配置校验未通过，共 {len(issues)} 项：\n{detail}")

    # 启动期生效配置回显（`A-16`）：线上出问题时，不用再翻代码 + 翻环境变量
    # 才知道「这次到底用的什么配置」。**脱敏后**才打印（key/token/secret/password
    # 一律 `***`，连接串里的口令段也脱敏）。
    logger.info(
        "生效配置: %s",
        json.dumps(settings.effective_summary(), ensure_ascii=False, sort_keys=True),
    )
    # 「配了但没生效」的提示：例如 MODEL_PROVIDER=deepseek 时，
    # 向量与重排实际回落到 dashscope（DeepSeek 没有那两个端点）——
    # 不说出来，运维会以为「换了模型，检索也换了」。
    for note in settings.provider_notes():
        logger.warning("配置提示: %s", note)

    # 生产环境关闭交互式 API 文档（`A-04`）。
    # 为什么是**按环境开关**而不是一刀切：`tests/e2e/test_all_endpoints.py` 依赖
    # OpenAPI 枚举端点，端点表与文档同步的守卫也依赖它——一刀切会把守卫一起打瞎。
    # 前端没有「接口文档」入口（已确认 `frontend/src` 无 /docs 引用），所以关掉不会
    # 产生「点进去 404」的次生问题。
    _is_prod = env_of(settings) in {"prod", "production"}
    app = FastAPI(
        title="Kquery 智能 RAG",
        version=_app_version(),
        lifespan=lifespan,
        docs_url=None if _is_prod else "/docs",
        openapi_url=None if _is_prod else "/openapi.json",
        redoc_url=None if _is_prod else "/redoc",
    )
    if _is_prod:
        logger.info("运行模式=prod：已关闭 /docs、/redoc、/openapi.json")

    # 装配层从 `app.state.settings` 取配置；在这里就位，
    # 保证不依赖「某个装配步骤碰巧先跑」
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_error_handlers(app)
    install_trace_middleware(app)
    # 路由注册：全部平铺挂在 app 上（角色门禁与 auth router 均已随认证下线删除）
    register_routes(app)
    return app
