"""知识库（KB）实体与用户授权。

判定树（与文档 ACL 五步同构，KB 粒度无 deny 名单、allow 仅用户粒度）：
1. 租户不匹配 → 拒绝（跨租户一律不可见）；
2. KB 不存在 → 拒绝（防探测：与「无权限」同语义）；
3. 命中授权名单（user_id）→ 放行；
4. 库公开（is_public）→ 放行（跨租户永不因此可见——第 1 步已拦截）；
5. 默认拒绝。

「变更影响人数」由后端反查（管理员不该凭用户名猜测影响面）。

设计要点：
- KB 判定被合并进 `dependencies.acl_filter_of` 的兜底闭包——后过滤是检索链路
  唯一的权限底线，只把 KB 加在下推 provider 上会在 provider 降级时越权；
- 缓存维度：KB 全局版本与 ACL 版本复合后进缓存签名（main.py 装配处），
  否则撤权后同用户的旧答案仍被命中；
- 默认知识库（kb_id == "default"）承接全部存量文档且 is_public=True，
  禁止删除。
"""
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import DocumentRecord, KnowledgeBase
from app.services.security.auth import Principal, UserDirectory

logger = logging.getLogger(__name__)

DEFAULT_KB_ID = "default"


class KBService:
    """知识库服务：KB 实体 CRUD + 授权关系 + 可见性判定。

    与 ACLService 同构：JSON 落盘 + 原子写 + 全局版本号 + 影响人数反查。
    正本是两份文件，内存只是镜像；损坏即拒绝启动（同 ACL 口径）。
    """

    def __init__(self, kb_path: Path, grants_path: Path, users: UserDirectory):
        self._kb_path = Path(kb_path)
        self._grants_path = Path(grants_path)
        self._lock = threading.Lock()
        # kb_id → KnowledgeBase；kb_id → list[dict(KbGrant)]
        self._kbs: dict[str, KnowledgeBase] = {}
        self._grants: dict[str, list[dict]] = {}
        self._directory = users
        # 全局版本：任何 KB/授权写操作递增，供缓存键携带——
        # 否则权限被撤销后旧缓存仍命中（缓存命中路径跳过检索与过滤）
        self._version = 0
        self._load()

    # ---- 持久化 ----

    def _load(self) -> None:
        if self._kb_path.exists():
            try:
                raw = json.loads(self._kb_path.read_text(encoding="utf-8"))
                self._kbs = {k["kb_id"]: KnowledgeBase.model_validate(k) for k in raw}
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                # 权限数据损坏比"没有权限数据"严重得多：拒绝以空 KB 对外服务
                logger.error("知识库数据损坏，拒绝启动: %s", exc)
                raise
        if self._grants_path.exists():
            try:
                raw = json.loads(self._grants_path.read_text(encoding="utf-8"))
                self._grants = {k: list(v) for k, v in raw.items()}
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                logger.error("知识库授权数据损坏，拒绝启动: %s", exc)
                raise
        # 首次启动（或文件丢失）：落默认 KB。存量文档的 knowledge_base_id
        # 回填由 KnowledgeService._load_metadata 完成，两边都以 default 为锚。
        if DEFAULT_KB_ID not in self._kbs:
            now = datetime.now(timezone.utc).isoformat()
            self._kbs[DEFAULT_KB_ID] = KnowledgeBase(
                kb_id=DEFAULT_KB_ID,
                name="默认知识库",
                description="系统默认知识库，承接未显式指定归属的全部文档",
                tenant_id=self._default_tenant(),
                is_public=True,
                created_by="system",
                created_at=now,
                updated_at=now,
            )
            self._save()

    def _default_tenant(self) -> str:
        tenants = self._directory.tenants() if hasattr(self._directory, "tenants") else []
        return tenants[0] if tenants else "default"

    def _save(self) -> None:
        # 原子写：临时文件 + replace。直接 write_text 时崩溃/并发写会留下半截 JSON，
        # 而启动校验是"损坏即拒绝启动"——半截文件等于把整个服务打挂。
        for path, payload in (
            (self._kb_path, [k.model_dump() for k in self._kbs.values()]),
            (self._grants_path, self._grants),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    # ---- 判定 ----

    def check(self, principal: Principal, kb_id: str) -> bool:
        """判定树：顺序不可调换。"""
        kb = self._kbs.get(kb_id)
        # 2. KB 不存在 → 拒绝（防探测）
        if kb is None:
            return False
        # 1. 租户闸门。默认 KB 例外：它是「各租户各自的公共桶」——
        # 新租户首次上传即隐式落在自己的 default 名下，跨租户泄漏由文档级
        # 闸门兜底（ACL 判定树第 1 步 doc.tenant_id != principal.tenant_id）。
        if kb_id != DEFAULT_KB_ID and kb.tenant_id != principal.tenant_id:
            return False
        # 3. 授权名单（用户粒度）
        grants = self._grants.get(kb_id, [])
        if any(g.get("user_id") == principal.user_id for g in grants):
            return True
        # 4. 租户内公开
        if kb.is_public:
            return True
        # 5. 默认拒绝
        return False

    def check_doc(self, principal: Principal, doc: DocumentRecord) -> bool:
        """文档级 KB 判定（供 acl_filter_of 闭包调用）。

        kb_id 取 doc.knowledge_base_id，空串归一化为默认 KB——存量文档
        经 KnowledgeService._load_metadata 回填后不会走到这里，兜底语义仍保留。
        """
        kb_id = doc.knowledge_base_id or DEFAULT_KB_ID
        return self.check(principal, kb_id)

    def visible_doc_ids(self, principal: Principal, docs: list[DocumentRecord]) -> set[str]:
        """KB 授权 → 展开为可见 doc_id 集合（供下推 provider 用）。

        注意：后过滤闭包才是兜底路径，本方法只服务下推优化，
        两条路径的判定必须同源（见 dependencies.acl_filter_of）。
        """
        return {d.doc_id for d in docs if self.check_doc(principal, d)}

    # ---- CRUD ----

    def get(self, kb_id: str) -> KnowledgeBase | None:
        with self._lock:
            return self._kbs.get(kb_id)

    @staticmethod
    def _in_tenant(kb: KnowledgeBase, tenant_id: str) -> bool:
        """租户归属判定：默认 KB 是各租户各自的公共桶，不租户过滤。"""
        return kb.kb_id == DEFAULT_KB_ID or kb.tenant_id == tenant_id

    def list_all(self, tenant_id: str | None = None) -> list[KnowledgeBase]:
        # 方法名避开内置 list：类体内的 list 会遮蔽类型注解里的 list[...]
        with self._lock:
            kbs = list(self._kbs.values())
        if tenant_id is not None:
            kbs = [k for k in kbs if self._in_tenant(k, tenant_id)]
        return sorted(kbs, key=lambda k: k.created_at)

    def create(
        self,
        name: str,
        description: str,
        is_public: bool,
        tenant_id: str,
        created_by: str,
    ) -> KnowledgeBase:
        with self._lock:
            if any(k.name == name and k.tenant_id == tenant_id for k in self._kbs.values()):
                raise ApiError(ErrCode.KB_DUPLICATED, f"知识库已存在: {name}", http_status=409)
            kb_id = self._new_kb_id(name)
            now = datetime.now(timezone.utc).isoformat()
            kb = KnowledgeBase(
                kb_id=kb_id,
                name=name,
                description=description,
                tenant_id=tenant_id,
                is_public=is_public,
                created_by=created_by,
                created_at=now,
                updated_at=now,
            )
            self._kbs[kb_id] = kb
            self._version += 1
            self._save()
            return kb

    def _new_kb_id(self, name: str) -> str:
        """kb_id 由名称推导（与 doc_id 同款 slug 规则），冲突时加短随机后缀。"""
        import re
        import uuid

        base = re.sub(r"[^\w一-鿿-]+", "-", name).strip("-").lower() or "kb"
        if base not in self._kbs:
            return base
        return f"{base}-{uuid.uuid4().hex[:8]}"

    def update(self, kb_id: str, patch: dict, updated_by: str) -> KnowledgeBase:
        with self._lock:
            kb = self._kbs.get(kb_id)
            if kb is None:
                raise ApiError(ErrCode.KB_NOT_FOUND, "无权限或不存在", http_status=404)
            # kb_id / tenant_id / created_by 不允许被改（主键语义与隔离边界）
            for key in ("name", "description", "is_public"):
                if key in patch:
                    setattr(kb, key, patch[key])
            kb.updated_at = datetime.now(timezone.utc).isoformat()
            self._version += 1
            self._save()
            return kb

    def delete(self, kb_id: str, doc_count: int) -> None:
        """删除 KB。doc_count 由调用方（KnowledgeService）反查，非空拒删。

        默认 KB 是存量文档的锚点，删除会让它们失锚——硬拒绝。
        """
        if kb_id == DEFAULT_KB_ID:
            raise ApiError(ErrCode.FORBIDDEN, "默认知识库禁止删除", http_status=400)
        with self._lock:
            if kb_id not in self._kbs:
                raise ApiError(ErrCode.KB_NOT_FOUND, "无权限或不存在", http_status=404)
            if doc_count > 0:
                raise ApiError(
                    ErrCode.KB_NOT_EMPTY,
                    f"知识库内仍有 {doc_count} 个文档，请先移出或删除",
                    http_status=400,
                )
            self._kbs.pop(kb_id)
            self._grants.pop(kb_id, None)
            self._version += 1
            self._save()

    # ---- 授权 ----

    def set_grants(self, kb_id: str, user_ids: list[str], granted_by: str) -> dict:
        """全量覆盖式授权。返回影响分析（后端反查）与风险等级（前端二次确认依据）。"""
        with self._lock:
            kb = self._kbs.get(kb_id)
            if kb is None:
                raise ApiError(ErrCode.KB_NOT_FOUND, "无权限或不存在", http_status=404)
            # 授权目标必须是真实存在且启用的用户——把授权发给不存在的账号
            # 等于把权限挂在空气上，事后排查"为什么这个人看不到"会非常难
            invalid = [u for u in user_ids if not self._grantable(u)]
            if invalid:
                raise ApiError(
                    ErrCode.KB_GRANT_INVALID,
                    f"授权目标用户不存在或已停用: {', '.join(invalid)}",
                    http_status=400,
                )
            old_users = {g.get("user_id") for g in self._grants.get(kb_id, [])}
            now = datetime.now(timezone.utc).isoformat()
            self._grants[kb_id] = [
                {"kb_id": kb_id, "user_id": u, "granted_by": granted_by, "granted_at": now}
                for u in sorted(set(user_ids))
            ]
            self._version += 1
            self._save()
        new_users = set(user_ids)
        added = sorted(new_users - old_users)
        removed = sorted(old_users - new_users)
        return {
            "kb_id": kb_id,
            "impact": {
                "added_count": len(added),
                "removed_count": len(removed),
                "added_users": added[:20],
                "removed_users": removed[:20],
                "total_after": len(new_users),
            },
            "risk": self._risk_level(len(old_users), len(new_users), kb.is_public),
        }

    def _grantable(self, user_id: str) -> bool:
        record = self._directory.get(user_id)
        return record is not None and record.get("enabled", True)

    @staticmethod
    def _risk_level(old_count: int, new_count: int, is_public: bool) -> str:
        """风险分级：扩权 = 高；缩权 = 低；改公开语义不在此（走 update）。"""
        if new_count > old_count:
            return "high"
        return "low"

    def grants_of(self, kb_id: str) -> list[dict]:
        """授权名单 + 用户启停状态反查。

        启停状态必须随响应回带：管理员对着一个已停用账号的授权记录做决策，
        本身就是错的（与 permission-matrix 反查真实用户数的理由相同）。
        """
        with self._lock:
            raw = list(self._grants.get(kb_id, []))
        out = []
        for g in raw:
            user_id = g.get("user_id", "")
            record = self._directory.get(user_id)
            out.append({
                "kb_id": g.get("kb_id", kb_id),
                "user_id": user_id,
                "granted_by": g.get("granted_by", ""),
                "granted_at": g.get("granted_at", ""),
                "user_enabled": bool(record and record.get("enabled", True)),
                "user_roles": (record or {}).get("roles", []),
            })
        return out

    def kbs_of_user(self, user_id: str, tenant_id: str) -> list[KnowledgeBase]:
        """反查：该用户可用的 KB 列表（用户管理页用）。"""
        principal = Principal(user_id=user_id, tenant_id=tenant_id)
        with self._lock:
            kbs = list(self._kbs.values())
        return [k for k in kbs if self._in_tenant(k, tenant_id) and self.check(principal, k.kb_id)]

    @property
    def version(self) -> int:
        """全局版本（缓存键维度之一）。"""
        return self._version
