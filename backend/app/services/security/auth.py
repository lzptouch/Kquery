"""认证（阶段 9 §2）：凭证 → 调用主体（Principal）。

- 提供者可替换：静态凭证表（开发/演示，严禁生产）/ 头部透传（默认兼容模式）；
- Principal 四要素：user_id / tenant_id（一切过滤的第一道闸门）/ roles / groups；
- ACL 匹配的是身份集合（user:/role:/group: 三类表达式的并集），不是用户名字符串。
"""
import logging
from dataclasses import dataclass, field
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


def build_auth_provider(enabled: bool, token_table: dict[str, dict]) -> AuthProvider:
    if enabled:
        logger.warning("认证启用：静态凭证表模式（仅限开发/演示，生产请换标准令牌）")
        return StaticTokenAuthProvider(token_table)
    return HeaderAuthProvider()
