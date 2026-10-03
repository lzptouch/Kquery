"""共享依赖：路由通过 Depends 获取 lifespan 装配在 app.state 上的服务。

依赖统一从这里出口，路由不直接触碰 app.state。
阶段 9：认证（get_principal）与配额（滑动窗口）在此统一叠加——
这正是当初"依赖统一出口"设计预留的挂载点。
"""
from fastapi import Request

from app.core.config import Settings
from app.core.envelope import ApiError, ErrCode
from app.services.agent.runner import AgentRunner
from app.services.agent.sessions import SessionStore
from app.services.knowledge import KnowledgeService
from app.services.pipeline import RagPipeline
from app.services.security.auth import Principal


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_pipeline(request: Request) -> RagPipeline:
    return request.app.state.pipeline


def get_knowledge_service(request: Request) -> KnowledgeService:
    return request.app.state.knowledge


def get_agent_runner(request: Request) -> AgentRunner:
    return request.app.state.agent_runner


def get_session_store(request: Request) -> SessionStore:
    return request.app.state.session_store


def get_orchestrator(request: Request):
    return request.app.state.orchestrator


def get_principal(request: Request) -> Principal:
    """认证：Bearer Token（启用时）或头部透传（默认兼容模式）。

    认证失败抛 1101（前端跳登录并清空本地缓存）。
    """
    auth = request.app.state.auth_provider
    authorization = request.headers.get("authorization", "")
    token = authorization[7:] if authorization.lower().startswith("bearer ") else None
    return auth.authenticate(token, dict(request.headers))


def enforce_rate_limit(request: Request) -> None:
    """配额：租户滑动窗口（分钟 + 每日双维度）。超额 429 + Retry-After。

    令牌桶（阶段 2 突发控制）与滑动窗口（阶段 9 平稳流量）并存：
    任一超限即拒绝。
    """
    settings: Settings = request.app.state.settings
    limiter = request.app.state.limiter
    quota = request.app.state.quota_service
    config = request.app.state.config_store.get_active()

    # 平台侧配额（账号级上游约束，先于租户配额判定）
    # 应用侧配额算得再精细，也无法阻止账号维度被平台限流——此处提前拦截。
    platform = request.app.state.platform_monitor
    if platform.exhausted():
        raise ApiError(
            ErrCode.QUOTA_EXCEEDED,
            "平台侧账号额度已用尽，影响全部租户，请次日重试或联系管理员扩容",
            http_status=429,
            retry_after=platform.seconds_until_reset(),
        )

    # 滑动窗口（租户维度，分钟+每日）
    tenant = request.headers.get("X-Tenant-Id", settings.default_tenant_id)
    allowed, retry_after, dimension = quota.check_and_consume(tenant)
    if not allowed:
        if dimension == "daily":
            raise ApiError(
                ErrCode.QUOTA_EXCEEDED,
                f"租户今日调用次数已用尽，次日恢复或联系管理员扩容",
                http_status=429,
                retry_after=retry_after,
            )
        raise ApiError(
            ErrCode.RATE_LIMITED,
            f"当前租户配额已用完，{retry_after} 秒后可重试",
            http_status=429,
            retry_after=retry_after,
        )

    # 令牌桶（突发控制，保持阶段 2 语义）
    allowed, retry_after = limiter.allow(
        settings.default_tenant_id, config.ratelimit_per_min, config.ratelimit_capacity
    )
    if not allowed:
        raise ApiError(
            ErrCode.RATE_LIMITED,
            f"当前租户配额已用完，{retry_after} 秒后可重试",
            http_status=429,
            retry_after=retry_after,
        )
