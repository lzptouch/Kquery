"""共享依赖：路由通过 Depends 获取 lifespan 装配在 app.state 上的服务。

依赖统一从这里出口，路由不直接触碰 app.state。
突发限流（令牌桶）在此统一叠加——这正是当初"依赖统一出口"设计预留的挂载点。

## 认证已下线

`get_principal` 不再解析任何凭证：注册 / 登录 / 口令 / JWT / 静态凭证 / 用户目录
整套认证能力已删除（详见 `backend/app/core/identity.py` 的模块说明）。本依赖保留
下来，是因为身份是**数据隔离维度**（文档归属 / 会话归属 / 缓存键 / 限流分桶 /
图谱与记忆分区），不是权限功能。

随认证一起下线的能力：

- `require_role`（角色门禁）——管理面端点不再校验角色；
- `acl_filter_of`（文档级 ACL + 知识库授权兜底闭包）——检索链路不再做文档级可见性判定。
"""

from fastapi import Request

from app.core.config import Settings
from app.core.envelope import ApiError, ErrCode
from app.core.identity import Principal
from app.services.agent.sessions import SessionStore
from app.services.knowledge import KnowledgeService
from app.services.pipeline import RagPipeline
from app.services.ratelimit import resolve_bucket_limits
from app.core.logging_setup import get_logger

logger = get_logger(__name__)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pipeline(request: Request) -> RagPipeline:
    return request.app.state.pipeline


def get_knowledge_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge


def get_session_store(request: Request) -> SessionStore:
    return request.app.state.session_store


def get_principal(request: Request) -> Principal:
    """请求身份（固定值）。

    认证已下线：不再读 `Authorization` 头、不再查用户目录、不再复核角色。
    保留该依赖是为了让 35 个端点的签名与数据隔离口径保持不变——
    端点里 `principal.tenant_id` / `principal.user_id` 仍在用，
    但它们的取值恒定。
    """
    principal = Principal()
    # 限流依赖按租户分桶，必须在配额判定之前就把主体挂到 request 上
    request.state.principal = principal
    return principal


def enforce_rate_limit(request: Request) -> None:
    """突发限流：租户令牌桶。超额 429 + Retry-After。

    租户取固定身份（认证已下线，租户恒为 `default`）。
    单租户下分桶维度不再区分来源——但**限流本身保留**：它防的是"打爆上游模型
    配额"，与是否有认证无关。

    说明：应用侧配额（分钟/每日/token 三维）与平台侧额度监控已下线，
    这里只保留令牌桶的突发保护。
    """
    settings: Settings = request.app.state.settings
    limiter = request.app.state.limiter
    config = request.app.state.config_store.get_active()

    principal = get_principal(request)
    tenant = principal.tenant_id or settings.default_tenant_id
    request.state.tenant_id = tenant

    # 令牌桶（突发控制；**按租户**分桶）
    rate, capacity = resolve_bucket_limits(config, settings)
    allowed, retry_after = limiter.allow(tenant, rate, capacity)
    if not allowed:
        raise ApiError(
            ErrCode.RATE_LIMITED,
            f"当前租户请求过于频繁，{retry_after} 秒后可重试",
            http_status=429,
            retry_after=retry_after,
        )


def reject_background_when_busy(request: Request) -> None:
    """低优先级端点的负载闸门（`C-11`）。

    ## 流量优先级

    | 优先级 | 端点 | 系统繁忙时的行为 |
    |---|---|---|
    | 高（交互） | `/ask`、`/ask/stream` | **继续接纳** |
    | 低（后台） | `/knowledge/ingest`、`/graph/build`、`/knowledge/rebuild` | **拒绝并返回 503 + `Retry-After`** |

    ## 为什么要有

    「后台工作把交互服务打挂」是最常见的容量事故：一次批量导入就能塞满
    共享的排队位，于是 `/ask` 全部 503——**用户以为服务坏了，其实只是有人在导数据**。
    有了优先级，后台任务在系统繁忙时让路，而不是拖着交互流量一起死。

    拒绝时给的是**明确的文案**（不是笼统的「服务繁忙」），运维从响应里就能
    判断这是背压而不是故障。
    """
    settings: Settings = request.app.state.settings
    ratio = float(getattr(settings, "background_reject_queue_ratio", 0.8))
    executor = getattr(request.app.state, "pipeline_executor", None)
    if executor is None:
        return
    stats = executor.stats()
    limit = max(1, int(stats.get("queue_size", 0) * ratio))
    waiting = int(stats.get("waiting", 0))
    if waiting < limit:
        return
    logger.warning(
        "后台任务被拒：排队深度 %d ≥ 阈值 %d（低优先级让路）", waiting, limit,
    )
    raise ApiError(
        ErrCode.RATE_LIMITED,
        "系统繁忙：后台任务队列已接近上限，请稍后重试",
        http_status=503,
        retry_after=5,
    )
