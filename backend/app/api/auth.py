"""登录 / 注册 / 当前身份（账号密码档）。

## 为什么不挂在 security.py

`security.py` 的整个 router 挂着 `require_role("admin")`（见该文件 25 行）。
把登录端点放进去等于要求"先有 admin 身份才能登录"——死锁。
本 router 因此**不挂任何角色门禁**：`/auth/login`、`/auth/register` 公开，
`/auth/me` 只挂 `get_principal`（认令牌即可，不需要 admin）。

## 与其它认证档位的关系

登录成功签发的是 `JwtAuthProvider.encode()` 产出的 HS256 令牌，
与外部 IdP 档位**同一种令牌**——前端处理方式一致，后端也不必为
"密码登录的令牌"单独开一条验签分支。

## 失败信息一律不区分原因

用户不存在 / 口令错误 / 账号已停用，三种情况返回**完全相同**的码（1001）、
文案与 HTTP 状态。区分其中任何一种，都会把"这个账号存在"变成可批量探测的信息。
代价是停用账号的用户看到的是"用户名或密码不正确"而非"账号已停用"——
这个代价换来不可枚举，值得。
"""
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.dependencies import get_principal
from app.services.ratelimit import TokenBucketLimiter
from app.services.security import password as pw
from app.services.security.auth import JwtAuthProvider, Principal

logger = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])

#: 登录失败限流：每分钟 6 次、桶容量 8。容量略大于速率以容忍手滑连点，
#: 但远低于人工输错的恢复速度——要挡的是脚本，不是手滑。
LOGIN_RATE_PER_MIN = 6.0
LOGIN_BUCKET = 8.0

#: 自助注册的新账号一律落到这个角色。`ROLE_CAPABILITIES` 中 sales 恰为
#: 最小能力集（ask + view_own_data），与"普通用户只能对话"的产品定义一致。
DEFAULT_ROLE = "sales"

#: 三种登录失败共用同一句文案，理由见模块说明
_LOGIN_FAILED_MESSAGE = "用户名或密码不正确"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


def _password_mode(request: Request) -> bool:
    """账号密码登录未启用时明确拒绝，而不是"看起来能用"。

    典型误配症状：部署时忘了开 `AUTH_PASSWORD_ENABLED`，provider 仍是
    HeaderAuthProvider——它完全不看令牌。此时若放任不管，登录会"成功"并
    签出一个匿名令牌，用户随后看到的是一片 403。故此处显式拦截。

    两个条件缺一不可，且都必须看：
    - `auth_password_enabled`：是否开启口令档；
    - `auth_enabled`：认证总开关。为 false 时 `build_auth_provider` 返回的是
      HeaderAuthProvider，它**没有** `encode` 方法。只判前者会让这个半开半关
      的组合（口令档开了但认证总开关没开）在登录时抛 AttributeError → 500，
      比"明确拒绝"难查得多。判定改为直接问 provider 要不要签发能力，
      与实际行为同源，不会再出现"配置说开了、代码却做不到"的偏差。
    """
    settings = request.app.state.settings
    if not getattr(settings, "auth_password_enabled", False):
        return False
    if not getattr(settings, "auth_enabled", True):
        return False
    return hasattr(request.app.state.auth_provider, "encode")


def _login_limiter(request: Request) -> TokenBucketLimiter:
    """登录专用限流器，**不复用** `app.state.limiter`。

    后者是租户问答配额桶（键为 tenant_id）。登录尝试若与它共用实例，
    连输错几次口令就会吃掉用户真正的问答配额——两个无关资源共用一个
    计数器，是很难被察觉的一类耦合。
    """
    limiter = getattr(request.app.state, "login_limiter", None)
    if limiter is None:
        limiter = TokenBucketLimiter()
        request.app.state.login_limiter = limiter
    return limiter


def _audit(request: Request, trace_id: str, event: str, user_id: str, detail: dict) -> None:
    """登录/注册走 `append` 而非 `log_admin`——后者语义是"管理员操作"。

    租户固定记为 `default`：登录发生时尚未认证，无可信租户来源；
    登录成功后的身份已由后续请求的审计覆盖。
    """
    audit_store = getattr(request.app.state, "audit_store", None)
    if audit_store is not None:
        audit_store.append(event, trace_id, "default", user_id, detail)


def _ttl(request: Request) -> int:
    return max(60, int(getattr(request.app.state.settings, "auth_session_ttl_s", 900)))


def _issue_token(request: Request, identity: dict) -> str:
    provider = request.app.state.auth_provider
    if not isinstance(provider, JwtAuthProvider):
        # 走到这里说明配置自相矛盾（开了密码模式但 provider 不是 JWT 档），
        # 属启动期本应拦下的情况；留一道显式错误好过静默发一个验不过的令牌。
        raise ApiError(
            ErrCode.INTERNAL_ERROR, "当前认证档位不支持自签发令牌", http_status=500
        )
    return provider.encode(
        {
            "sub": identity["user_id"],
            "tenant_id": identity["tenant_id"],
            "roles": identity["roles"],
            "groups": identity["groups"],
        },
        _ttl(request),
    )


def _login_failed(request: Request, trace_id: str, username: str, reason: str) -> ApiError:
    _audit(request, trace_id, "auth.login", username, {"result": reason})
    return ApiError(ErrCode.AUTH_FAILED, _LOGIN_FAILED_MESSAGE, http_status=401)


@router.post("/auth/login", response_model=Envelope)
def login(req: LoginRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()

    if not _password_mode(request):
        raise ApiError(ErrCode.FORBIDDEN, "账号密码登录未启用", http_status=403)

    directory = request.app.state.user_directory

    # 限流键用登录名：按 IP 限流在本项目里没有意义（反向代理下取不到真实 IP），
    # 按登录名则刚好挡住"针对某个账号爆破"，且不影响其他用户。
    allowed, retry_after = _login_limiter(request).allow(
        f"login:{req.username.lower()}", LOGIN_RATE_PER_MIN, LOGIN_BUCKET
    )
    if not allowed:
        _audit(request, trace_id, "auth.login", req.username, {"result": "throttled"})
        raise ApiError(
            ErrCode.RATE_LIMITED, "登录尝试过于频繁，请稍后再试",
            http_status=429, retry_after=retry_after,
        )

    record = directory.find_by_username(req.username)
    if record is None:
        # 关键：账号不存在时也要跑一次等价强度的派生运算，否则"不存在"会因少一次
        # PBKDF2 而快几十倍，攻击者据此即可枚举账号。见 password.burn 的说明。
        pw.burn(req.password)
        raise _login_failed(request, trace_id, req.username, "unknown_user")
    if not pw.verify_password(req.password, record.get("password_hash")):
        raise _login_failed(request, trace_id, req.username, "bad_password")
    if not record.get("enabled", True):
        # 与上面两种失败同码同文案——单独提示"已停用"等于确认账号存在
        raise _login_failed(request, trace_id, req.username, "disabled")

    identity = directory.identity_of(req.username)
    token = _issue_token(request, identity)
    _audit(request, trace_id, "auth.login", identity["user_id"], {"result": "ok"})
    return ok_envelope(
        {
            "token": token,
            "user_id": identity["user_id"],
            "tenant_id": identity["tenant_id"],
            "roles": identity["roles"],
            "groups": identity["groups"],
            "expires_in": _ttl(request),
        },
        trace_id,
        elapsed_ms_since(started),
    )


@router.post("/auth/register", response_model=Envelope)
def register(req: RegisterRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()

    settings = request.app.state.settings
    if not _password_mode(request):
        raise ApiError(ErrCode.FORBIDDEN, "账号密码登录未启用", http_status=403)
    if not settings.auth_registration_enabled:
        raise ApiError(
            ErrCode.FORBIDDEN, "自助注册未开放，请联系管理员开通账号", http_status=403
        )

    weak = pw.validate_password_strength(req.password)
    if weak:
        # 口令太弱是请求参数问题（2004/400），与"凭证不对"（1001/401）分开：
        # 前者要改密码，后者重输即可，混在一起会让用户反复重输弱口令。
        raise ApiError(ErrCode.INVALID_INPUT, weak, http_status=400)

    username = req.username.strip()
    if not username:
        raise ApiError(ErrCode.INVALID_INPUT, "用户名不能为空", http_status=400)

    directory = request.app.state.user_directory
    if directory.find_by_username(username) is not None:
        raise ApiError(ErrCode.INVALID_INPUT, "该用户名已被占用", http_status=409)

    # 角色写死，不接受请求体里的 roles——否则任何人都能自助注册成 admin
    directory.upsert(
        username,
        {
            "roles": [DEFAULT_ROLE],
            "groups": [],
            "enabled": True,
            "tenant_id": settings.default_tenant_id,
            "password_hash": pw.hash_password(req.password),
        },
    )
    _audit(request, trace_id, "auth.register", username, {"roles": [DEFAULT_ROLE]})
    logger.info("新账号已注册: %s（角色 %s）", username, DEFAULT_ROLE)
    return ok_envelope(
        {"user_id": username, "roles": [DEFAULT_ROLE]},
        trace_id,
        elapsed_ms_since(started),
    )


@router.get("/auth/me", response_model=Envelope)
def me(request: Request, principal: Principal = Depends(get_principal)) -> Envelope:
    """当前身份。只要求已认证，不要求 admin——前端据此决定显隐。

    顺带回带 `is_admin`：前端要判断"能否进管理区"，
    没必要自己实现一遍 `"admin" in roles`（各页面各写一遍必然不同步）。
    """
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    return ok_envelope(
        {
            "user_id": principal.user_id,
            "tenant_id": principal.tenant_id,
            "roles": list(principal.roles),
            "groups": list(principal.groups),
            "is_admin": principal.is_admin,
        },
        trace_id,
        elapsed_ms_since(started),
    )
