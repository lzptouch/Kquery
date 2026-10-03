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


class ACLService:
    def __init__(self, acl_path: Path, users: list[dict]):
        self._path = acl_path
        self._lock = threading.Lock()
        # doc_id → {allow, deny, is_public, updated_at, updated_by}
        self._acl: dict[str, dict] = {}
        self._users = users  # 用户目录（影响人数反查）
        if acl_path.exists():
            try:
                self._acl = json.loads(acl_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                logger.warning("ACL 存储加载失败，从空开始")

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

    # ---- 管理 ----

    def get_acl(self, doc_id: str) -> dict:
        return self._acl.get(doc_id, {"allow": [], "deny": [], "is_public": None})

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
            self._save()
        return {
            "before": old,
            "after": new,
            "impact": self._impact(old, new),
            "risk": self._risk_level(old, new),
        }

    def _identities_to_users(self, identities: list[str]) -> set[str]:
        """身份表达式 → 覆盖的用户集合（反查）。"""
        users: set[str] = set()
        for ident in identities:
            kind, _, value = ident.partition(":")
            for u in self._users:
                if not u.get("enabled", True):
                    continue
                if kind == "user" and u["user_id"] == value:
                    users.add(u["user_id"])
                elif kind == "role" and value in u.get("roles", []):
                    users.add(u["user_id"])
                elif kind == "group" and value in u.get("groups", []):
                    users.add(u["user_id"])
        return users

    def _impact(self, old: dict, new: dict) -> dict:
        old_users = self._identities_to_users(old.get("allow", []))
        new_users = self._identities_to_users(new.get("allow", []))
        if old.get("is_public", True):
            old_users |= {u["user_id"] for u in self._users if u.get("enabled", True)}
        if new.get("is_public", True):
            new_users |= {u["user_id"] for u in self._users if u.get("enabled", True)}
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
