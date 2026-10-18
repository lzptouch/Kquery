"""认证（阶段 9 §2）：凭证 → 调用主体（Principal）。

- 提供者可替换且分三档：标准令牌 JWT（企业生产）/ 静态凭证表（开发演示）/
  头部透传（默认兼容）；
- Principal 四要素：user_id / tenant_id（一切过滤的第一道闸门）/ roles / groups；
- ACL 匹配的是身份集合（user:/role:/group: 三类表达式的并集），不是用户名字符串。
"""
import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.core.envelope import ApiError, ErrCode

logger = logging.getLogger(__name__)


@dataclass
class Principal:
    """认证后的调用主体：身份集合的载体。"""

    user_id: str
    tenant_id: str
    roles: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def identities(self) -> set[str]:
        """身份集合：ACL 匹配的输入（三类粒度取并集）。"""
        return {
            f"user:{self.user_id}",
            *(f"role:{r}" for r in self.roles),
            *(f"group:{g}" for g in self.groups),
        }

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


class AuthProvider(Protocol):
    def authenticate(self, token: str | None, headers: dict) -> Principal: ...


class HeaderAuthProvider:
    """头部透传（默认，向后兼容）：X-Tenant-Id / X-User-Id / X-Roles / X-Groups。

    仅供开发与演示——不验证任何凭证，生产必须换标准令牌提供者。
    """

    def authenticate(self, token: str | None, headers: dict) -> Principal:
        return Principal(
            user_id=headers.get("x-user-id", "anon"),
            tenant_id=headers.get("x-tenant-id", "default"),
            roles=[r.strip() for r in headers.get("x-roles", "").split(",") if r.strip()],
            groups=[g.strip() for g in headers.get("x-groups", "").split(",") if g.strip()],
        )


class StaticTokenAuthProvider:
    """静态凭证表（token → Principal）。开发/内部演示用，严禁生产。"""

    def __init__(self, token_table: dict[str, dict]):
        # token → {user_id, tenant_id, roles, groups, enabled}
        self._table = token_table

    def authenticate(self, token: str | None, headers: dict) -> Principal:
        if not token:
            raise ApiError(ErrCode.UNAUTHORIZED, "缺少访问凭证", http_status=401)
        record = self._table.get(token)
        if record is None:
            raise ApiError(ErrCode.UNAUTHORIZED, "凭证无效或已过期", http_status=401)
        if not record.get("enabled", True):
            raise ApiError(ErrCode.UNAUTHORIZED, "账号已停用", http_status=401)
        return Principal(
            user_id=record["user_id"],
            tenant_id=record["tenant_id"],
            roles=record.get("roles", []),
            groups=record.get("groups", []),
            enabled=True,
        )


class JwtAuthProvider:
    """标准令牌档（HS256 JWT，企业生产用）。

    刻意**不引第三方库**：JWT 的验签只需要 HMAC-SHA256 + base64url，
    自己实现可以把"接受哪些算法 / 校验哪些声明"写在代码里而不是藏进依赖。
    必须显式拒绝 alg=none 与算法混淆（用密钥当作 HMAC 密钥时尤其危险）。
    """

    def __init__(self, secret: str, issuer: str = "", audience: str = "", leeway_s: int = 30):
        if len(secret) < 16:
            raise ApiError(
                ErrCode.INVALID_REQUEST, "JWT 密钥过短（至少 16 字符）", http_status=500
            )
        self._secret = secret.encode("utf-8")
        self._issuer = issuer
        self._audience = audience
        self._leeway = leeway_s

    @staticmethod
    def _b64url_decode(segment: str) -> bytes:
        padding = "=" * (-len(segment) % 4)
        return base64.urlsafe_b64decode(segment + padding)

    @staticmethod
    def _b64url_encode(raw: bytes) -> str:
        """与 `_b64url_decode` 对称。JWT 要求无 padding，故去掉尾部 `=`。"""
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _verify_signature(self, signing_input: bytes, signature: bytes) -> bool:
        expected = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return hmac.compare_digest(expected, signature)

    def encode(self, payload: dict, ttl_s: int) -> str:
        """自签发 HS256 令牌——账号密码登录成功后用它换取后续请求的身份。

        与 `decode` 严格成对：这里写入的每一条声明都是 `decode` 会回头校验的。
        少写一条就会出现"签发成功、下一次请求立刻 401"的组合，且日志里看不出原因，
        所以 `iss`/`aud`/`exp`/`sub` 必须按 `decode` 的期望逐条对齐，而不是各写各的。
        """
        now = int(time.time())
        claims = dict(payload)
        # decode 强制要求 sub（否则 152 行抛错），这里兜住"调用方忘了给"的情况
        claims["sub"] = str(claims.get("sub") or "")
        claims.setdefault("iat", now)
        claims["exp"] = now + int(ttl_s)
        if self._issuer:
            claims["iss"] = self._issuer
        if self._audience:
            claims["aud"] = self._audience
        # compact JSON：分隔符无空格、键排序——保证同一份声明签两次结果一致，便于排查
        def _seg(obj: dict) -> str:
            return self._b64url_encode(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8"))

        signing_input = f"{_seg({'alg': 'HS256', 'typ': 'JWT'})}.{_seg(claims)}".encode("ascii")
        signature = hmac.new(self._secret, signing_input, hashlib.sha256).digest()
        return f"{signing_input.decode('ascii')}.{self._b64url_encode(signature)}"

    def decode(self, token: str) -> dict:
        parts = token.split(".")
        if len(parts) != 3:
            raise ApiError(ErrCode.UNAUTHORIZED, "令牌格式非法", http_status=401)
        header_b64, payload_b64, sig_b64 = parts
        try:
            header = json.loads(self._b64url_decode(header_b64))
            payload = json.loads(self._b64url_decode(payload_b64))
            signature = self._b64url_decode(sig_b64)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ApiError(ErrCode.UNAUTHORIZED, "令牌解析失败", http_status=401) from exc

        alg = header.get("alg")
        if alg != "HS256":
            # alg=none / RS256 混淆都必须拒绝，否则签名校验形同虚设
            raise ApiError(
                ErrCode.UNAUTHORIZED, f"不支持的签名算法 {alg}（仅接受 HS256）", http_status=401
            )
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        if not self._verify_signature(signing_input, signature):
            raise ApiError(ErrCode.AUTH_FAILED, "令牌签名校验失败", http_status=401)

        now = time.time()
        exp = payload.get("exp")
        if exp is not None and now > float(exp) + self._leeway:
            raise ApiError(ErrCode.UNAUTHORIZED, "令牌已过期", http_status=401)
        nbf = payload.get("nbf")
        if nbf is not None and now + self._leeway < float(nbf):
            raise ApiError(ErrCode.UNAUTHORIZED, "令牌尚未生效", http_status=401)
        if self._issuer and payload.get("iss") != self._issuer:
            raise ApiError(ErrCode.UNAUTHORIZED, "签发方不匹配", http_status=401)
        if self._audience and payload.get("aud") != self._audience:
            raise ApiError(ErrCode.UNAUTHORIZED, "受众不匹配", http_status=401)
        if not payload.get("sub"):
            raise ApiError(ErrCode.UNAUTHORIZED, "令牌缺少主体声明 sub", http_status=401)
        return payload

    def authenticate(self, token: str | None, headers: dict) -> Principal:
        if not token:
            raise ApiError(ErrCode.UNAUTHORIZED, "缺少访问凭证", http_status=401)
        payload = self.decode(token)
        return Principal(
            user_id=str(payload["sub"]),
            tenant_id=str(payload.get("tenant_id") or payload.get("tenant") or "default"),
            roles=[str(r) for r in payload.get("roles", [])],
            groups=[str(g) for g in payload.get("groups", [])],
            enabled=True,
        )


class UserDirectory:
    """用户目录（阶段 9 §8.1 Tenant/User 实体）。

    此前用户目录是 lifespan 里的硬编码字面量，`PUT /admin/users/{id}` 只改内存，
    重启即丢——ACL 反查影响人数时拿到的也是这份临时数据。改为落盘 JSON。
    """

    DEFAULT_USERS = [
        {"user_id": "admin", "tenant_id": "default", "roles": ["admin"], "groups": [], "enabled": True},
        {"user_id": "zhangsan", "tenant_id": "default", "roles": ["hr"], "groups": [], "enabled": True},
        {"user_id": "lisi", "tenant_id": "default", "roles": ["sales"], "groups": [], "enabled": True},
        {"user_id": "wangwu", "tenant_id": "tenant-b", "roles": ["finance"], "groups": ["exec"], "enabled": True},
    ]

    def __init__(self, path: Path, initial: list[dict] | None = None):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._users: dict[str, dict] = {}
        self._load(initial)

    def _load(self, initial: list[dict] | None) -> None:
        raw: list[dict] | None = None
        if self._path.exists():
            try:
                loaded = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    raw = loaded
            except (OSError, json.JSONDecodeError) as exc:
                logger.error("用户目录文件损坏，拒绝以脏数据启动: %s", exc)
                raise
        if raw is None:
            raw = initial if initial is not None else self.DEFAULT_USERS
            self._persist(raw)
        for record in raw:
            if isinstance(record, dict) and record.get("user_id"):
                self._users[record["user_id"]] = dict(record)

    def _persist(self, users: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)  # 原子替换，避免读到半截文件

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(u) for u in self._users.values()]

    def tenants(self) -> list[str]:
        with self._lock:
            return sorted({u.get("tenant_id", "default") for u in self._users.values()})

    def get(self, user_id: str) -> dict | None:
        with self._lock:
            record = self._users.get(user_id)
            return dict(record) if record else None

    def find_by_username(self, username: str) -> dict | None:
        """按登录名查账号。

        登录名**就是** `user_id` 主键，刻意不引入独立的 `username` 字段：
        多一套标识就意味着要处理重名映射、改名后审计记录失联、
        ACL 里的 `user:` 表达式与登录名不一致等一连串问题，收益为零。
        """
        if not username:
            return None
        return self.get(username)

    def identity_of(self, user_id: str) -> dict | None:
        """取签发令牌所需的身份四要素（**不含** password_hash）。"""
        record = self.get(user_id)
        if record is None:
            return None
        return {
            "user_id": record.get("user_id", user_id),
            "tenant_id": record.get("tenant_id") or "default",
            "roles": list(record.get("roles") or []),
            "groups": list(record.get("groups") or []),
        }

    def set_password(self, user_id: str, encoded: str) -> dict:
        """写入口令哈希。账号必须已存在——**不隐式建号**。

        隐式建号会让"给 zhangsan 设密码"意外产生一个新账号，
        而管理员以为自己只是改了一个人的密码。
        """
        if self.get(user_id) is None:
            raise KeyError(f"用户不存在: {user_id}")
        return self.upsert(user_id, {"password_hash": encoded})

    def upsert(self, user_id: str, patch: dict) -> dict:
        """新增或更新。user_id 与 tenant_id 不允许被 patch 改掉（它们是主键语义）。"""
        with self._lock:
            record = self._users.get(user_id) or {
                "user_id": user_id,
                "tenant_id": "default",
                "roles": [],
                "groups": [],
                "enabled": True,
            }
            for key, value in patch.items():
                if key in {"user_id", "tenant_id"}:
                    continue
                record[key] = value
            self._users[user_id] = record
            self._persist([dict(u) for u in self._users.values()])
            return dict(record)

    def matches(self, expression: str) -> bool:
        """给定身份表达式（user:/role:/group:）判断目录里是否存在匹配用户。"""
        kind, _, value = expression.partition(":")
        if not value:
            return False
        with self._lock:
            for record in self._users.values():
                if not record.get("enabled", True):
                    continue
                if kind == "user" and record.get("user_id") == value:
                    return True
                if kind == "role" and value in (record.get("roles") or []):
                    return True
                if kind == "group" and value in (record.get("groups") or []):
                    return True
            return False

    def identities_to_users(self, expressions: list[str]) -> list[dict]:
        """把身份表达式集合展开为具体用户（影响人数预览靠这个，必须真实反查）。"""
        wanted = set(expressions)
        out: list[dict] = []
        with self._lock:
            for record in self._users.values():
                ids = {
                    f"user:{record.get('user_id', '')}",
                    *(f"role:{r}" for r in record.get("roles") or []),
                    *(f"group:{g}" for g in record.get("groups") or []),
                }
                if ids & wanted:
                    out.append(dict(record))
        return out


def build_auth_provider(
    enabled: bool,
    token_table: dict[str, dict],
    jwt_settings=None,
) -> AuthProvider:
    """认证档位：JWT（含账号密码自签发）> 静态凭证表 > 头部透传。

    账号密码模式与外部 IdP 模式**共用 `JwtAuthProvider`**——两者产出的都是
    HS256 令牌，验签路径完全相同。拆成两个 Provider 只会多出一处
    "有 token 时验签、无 token 时怎么办"的分支歧义，而那条分支在两种模式下
    的答案都是一样的：不回落身份头（回落就等于给未认证请求开后门）。
    """
    password_enabled = bool(getattr(jwt_settings, "auth_password_enabled", False))
    if enabled and jwt_settings is not None and (jwt_settings.auth_jwt_enabled or password_enabled):
        if password_enabled:
            logger.info("认证启用：账号密码登录模式（登录自签发 HS256 令牌）")
        else:
            logger.info("认证启用：标准令牌模式（HS256 JWT）")
        if password_enabled and token_table:
            # 静态表会被完全忽略，必须明说——否则排查"我配的 token 为什么不生效"很痛苦
            logger.warning("已启用账号密码登录，auth_tokens_json 静态凭证表被忽略")
        return JwtAuthProvider(
            jwt_settings.auth_jwt_secret,
            issuer=jwt_settings.auth_jwt_issuer,
            audience=jwt_settings.auth_jwt_audience,
        )
    if enabled:
        logger.warning("认证启用：静态凭证表模式（仅限开发/演示，生产请启用 auth_jwt_enabled）")
        return StaticTokenAuthProvider(token_table)
    return HeaderAuthProvider()
