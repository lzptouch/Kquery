"""文档级 ACL（阶段 9 §3）。

判定顺序（短路，不可调换）：
1. 租户不匹配 → 拒绝（跨租户一律不可见）；
2. 命中拒绝名单 → 拒绝（deny 优先于 allow：角色授权不能救回被拉黑者）；
3. 命中授权名单（user/role/group 任一）→ 放行；
4. 租户内公开 → 放行（跨租户永不因此可见——第 1 步已拦截）；
5. 默认拒绝。

"变更影响人数"由后端反查（管理员不该凭角色名猜测影响面）。
"""
import json
import logging
import threading
import time
from pathlib import Path

from app.domain.schemas import DocumentRecord
from app.services.security.auth import Principal

logger = logging.getLogger(__name__)


class _StaticDirectory:
    """旧调用兼容层：把 list[dict] 包成目录接口（不持久化）。"""

    def __init__(self, users: list[dict]):
        self._users = list(users)

    def all(self) -> list[dict]:
        return [dict(u) for u in self._users]

    def matches(self, expression: str) -> bool:
        kind, _, value = expression.partition(":")
        return any(
            (kind == "user" and u.get("user_id") == value)
            or (kind == "role" and value in (u.get("roles") or []))
            or (kind == "group" and value in (u.get("groups") or []))
            for u in self._users
            if u.get("enabled", True)
        )

    def identities_to_users(self, expressions: list[str]) -> list[dict]:
        wanted = set(expressions)
        out = []
        for u in self._users:
            ids = {
                f"user:{u.get('user_id', '')}",
                *(f"role:{r}" for r in u.get("roles") or []),
                *(f"group:{g}" for g in u.get("groups") or []),
            }
            if ids & wanted:
                out.append(dict(u))
        return out


class ACLService:
    def __init__(self, acl_path: Path, users):
        """`users` 为 `UserDirectory` 或 `list[dict]`（兼容旧调用）。

        走 UserDirectory 才有持久化与启停状态；给 list 时退化为静态目录。
        """
        self._path = acl_path
        self._lock = threading.Lock()
        # doc_id → {allow, deny, is_public, updated_at, updated_by}
        self._acl: dict[str, dict] = {}
        self._directory = users if hasattr(users, "all") else _StaticDirectory(users or [])
        # ACL 全局版本（DEF-R-1）：任何 ACL 写操作递增，供缓存键携带——
        # 否则权限被撤销后旧缓存仍命中（缓存命中路径跳过检索与 ACL 过滤）
        self._version = 0
        if acl_path.exists():
            try:
                self._acl = json.loads(acl_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                # 权限数据损坏比"没有权限数据"严重得多：拒绝以空 ACL 对外服务
                logger.error("ACL 存储损坏，拒绝启动: %s", exc)
                raise

    # ---- 判定 ----

    def check(self, principal: Principal, doc: DocumentRecord) -> bool:
        """判定树（§10.2）：顺序不可调换。"""
        acl = self._acl.get(doc.doc_id, {})
        # 1. 租户闸门
        if doc.tenant_id != principal.tenant_id:
            return False
        # 2. 拒绝名单优先
        deny = set(acl.get("deny", []))
        if principal.identities & deny:
            return False
        # 3. 授权名单（三类粒度并集）
        allow = set(acl.get("allow", []))
        if principal.identities & allow:
            return True
        # 4. 租户内公开（文档默认字段 + ACL 覆盖）
        is_public = acl.get("is_public", doc.acl_public)
        if is_public:
            return True
        # 5. 默认拒绝
        return False

    def filter_docs(self, principal: Principal, docs: list[DocumentRecord]) -> tuple[list[DocumentRecord], int]:
        """检索后过滤：返回 (可见文档, 被过滤数)。"""
        visible = [d for d in docs if self.check(principal, d)]
        return visible, len(docs) - len(visible)

    def visible_doc_ids(self, principal: Principal, docs: list[DocumentRecord]) -> set[str] | None:
        """当前主体在给定文档集内的可见文档 ID 集合（TASK-142 ACL 下推的输入）。

        用于把 ACL 下推到向量库/关键词检索，让不可见文档**不参与 top-k 竞争**
        （此前是后过滤，不可见文档会占满名额，DEF-R-6）。

        **代价说明**：这是 O(文档数) 的枚举，因此调用方应对大库保持克制——
        返回 None 表示"不建议下推"（此时退回 tenant 粗过滤 + 应用层后过滤）。
        阈值取 5000：再大时 Milvus 表达式也会因超长而降级，枚举本身也变贵。
        """
        if len(docs) > 5000:
            return None
        return {d.doc_id for d in docs if self.check(principal, d)}

    # ---- 管理 ----

    def get_acl(self, doc_id: str) -> dict:
        return self._acl.get(doc_id, {"allow": [], "deny": [], "is_public": None})

    @property
    def version(self) -> int:
        """ACL 全局版本（缓存键维度之一）。"""
        return self._version

    def set_acl(
        self, doc_id: str, allow: list[str], deny: list[str], is_public: bool, updated_by: str
    ) -> dict:
        """设置 ACL 并返回影响分析（受影响人数由后端反查，§3.2 工程要点）。"""
        with self._lock:
            old = self._acl.get(doc_id, {"allow": [], "deny": [], "is_public": True})
            new = {
                "allow": allow, "deny": deny, "is_public": is_public,
                "updated_at": time.time(), "updated_by": updated_by,
            }
            self._acl[doc_id] = new
            self._version += 1
            self._save()
        return {
            "before": old,
            "after": new,
            "impact": self._impact(old, new),
            "risk": self._risk_level(old, new),
        }

    def _identities_to_users(self, identities: list[str]) -> set[str]:
        """身份表达式 → 覆盖的用户集合（反查，走用户目录的真实启停状态）。"""
        return {
            u["user_id"]
            for u in self._directory.identities_to_users(list(identities))
            if u.get("enabled", True)
        }

    def _enabled_user_ids(self) -> set[str]:
        return {u["user_id"] for u in self._directory.all() if u.get("enabled", True)}

    def _impact(self, old: dict, new: dict) -> dict:
        old_users = self._identities_to_users(old.get("allow", []))
        new_users = self._identities_to_users(new.get("allow", []))
        # 未设 is_public 时与判定树保持同方向（默认拒绝）：不并入全量 enabled 用户
        if old.get("is_public", False):
            old_users |= self._enabled_user_ids()
        if new.get("is_public", False):
            new_users |= self._enabled_user_ids()
        denied_users = self._identities_to_users(new.get("deny", []))
        new_users -= denied_users
        added = sorted(new_users - old_users)
        removed = sorted(old_users - new_users)
        return {
            "added_count": len(added), "removed_count": len(removed),
            "added_users": added[:20], "removed_users": removed[:20],
            "total_after": len(new_users),
        }

    @staticmethod
    def _risk_level(old: dict, new: dict) -> str:
        """风险分级：移除拒绝名单 / 改为公开 = 最高；扩大授权 = 高；缩小 = 低。"""
        if len(new.get("deny", [])) < len(old.get("deny", [])):
            return "critical"  # 移除黑名单 = 重新放权
        if new.get("is_public") and not old.get("is_public", True):
            return "critical"  # 改为公开 = 全体可见
        if len(new.get("allow", [])) > len(old.get("allow", [])):
            return "high"
        return "low"

    def list_acl(self, docs: list[DocumentRecord]) -> list[dict]:
        return [
            {
                "doc_id": d.doc_id, "filename": d.filename, "tenant_id": d.tenant_id,
                **self.get_acl(d.doc_id),
                "effective_public": self._acl.get(d.doc_id, {}).get("is_public", d.acl_public),
            }
            for d in docs
        ]

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._acl, ensure_ascii=False), encoding="utf-8")
