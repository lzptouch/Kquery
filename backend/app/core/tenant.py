"""多租户支持模块。

提供租户识别、数据隔离和配额管理能力。
基于现有kb_isolation.py的知识库隔离，扩展为完整的租户体系。
归属于跨层通用模块。

使用示例:
    from app.core.tenant import tenant_context, get_current_tenant
    
    @router.get("/collections")
    async def list_collections(
        tenant: TenantContext = Depends(get_current_tenant),
    ):
        collections = await service.list_collections(tenant.id)
        return collections
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

from app.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class TenantContext:
    """租户上下文。

    Attributes:
        tenant_id: 租户唯一标识。
        name: 租户名称。
        plan: 套餐类型（basic/pro/enterprise）。
        created_at: 创建时间。
        settings: 租户级配置。
    """
    tenant_id: str
    name: str = ""
    plan: str = "basic"
    created_at: str = ""
    settings: Dict[str, Any] = field(default_factory=dict)


# 线程安全的租户上下文
_tenant_context: ContextVar[Optional[TenantContext]] = ContextVar(
    "tenant_context", default=None
)


def set_current_tenant(tenant: Optional[TenantContext]) -> None:
    """设置当前请求的租户上下文。

    Args:
        tenant: 租户上下文，None表示无租户（系统级）。
    """
    _tenant_context.set(tenant)


def get_current_tenant() -> Optional[TenantContext]:
    """获取当前请求的租户上下文。

    Returns:
        租户上下文，无租户时返回None。
    """
    return _tenant_context.get()


class TenantService:
    """租户服务。

    管理租户的创建、查询和配置管理。
    """

    def __init__(self) -> None:
        self._tenants: Dict[str, TenantContext] = {}
        self._initialized = False

    def initialize_default_tenant(self) -> TenantContext:
        """初始化默认租户。

        Returns:
            默认租户上下文。
        """
        default = TenantContext(
            tenant_id="default",
            name="默认租户",
            plan="enterprise",
            created_at=datetime.utcnow().isoformat(),
            settings={
                "max_users": 100,
                "max_collections": 50,
                "max_documents": 10000,
                "enable_knowledge_graph": True,
                "enable_multi_modal": True,
            },
        )
        self._tenants["default"] = default
        self._initialized = True
        logger.info("默认租户已初始化")
        return default

    async def get_tenant(self, tenant_id: str) -> Optional[TenantContext]:
        """获取租户信息。

        Args:
            tenant_id: 租户ID。

        Returns:
            租户上下文，不存在返回None。
        """
        if not self._initialized:
            self.initialize_default_tenant()
        return self._tenants.get(tenant_id)

    async def create_tenant(
        self,
        name: str,
        plan: str = "basic",
        settings: Optional[Dict[str, Any]] = None,
    ) -> TenantContext:
        """创建新租户。

        Args:
            name: 租户名称。
            plan: 套餐类型。
            settings: 租户配置。

        Returns:
            新创建的租户上下文。
        """
        tenant = TenantContext(
            tenant_id=str(uuid.uuid4()),
            name=name,
            plan=plan,
            created_at=datetime.utcnow().isoformat(),
            settings=settings or {},
        )
        self._tenants[tenant.tenant_id] = tenant
        logger.info(f"租户已创建: {tenant.tenant_id} ({name})")
        return tenant

    def get_quota(self, tenant_id: str) -> Dict[str, Any]:
        """获取租户配额。

        Args:
            tenant_id: 租户ID。

        Returns:
            租户配额字典。
        """
        tenant = self._tenants.get(tenant_id)
        if tenant is None:
            return {}
        return tenant.settings

    def get_all_tenants(self) -> list[TenantContext]:
        """获取所有租户列表。"""
        return list(self._tenants.values())

    def get_tenant_namespace(self, tenant_id: str) -> str:
        """获取租户命名空间前缀。

        用于Milvus集合、Neo4j图空间、Redis key的隔离。
        """
        return f"tenant_{tenant_id}"


# 全局单例
tenant_service = TenantService()
