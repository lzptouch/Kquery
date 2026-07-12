"""CRM系统适配器。

采用策略模式支持多CRM源（Salesforce、纷享销客、自建CRM等）。
归属于L6连接集成层。

使用示例:
    from app.services.integration.crm_adapter import (
        SalesforceAdapter, FenxiangAdapter, CRMAdapterFactory
    )

    crm = CRMAdapterFactory.create_adapter("salesforce", {
        "base_url": "https://instance.salesforce.com",
        "client_id": "...",
        "client_secret": "...",
    })
    result = await crm.query_contacts(query="腾讯公司")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Type

from app.core.logger import get_logger
from app.services.integration.base import (
    ExternalServiceAdapter,
    ServiceRequest,
    ServiceResponse,
)

logger = get_logger(__name__)


@dataclass
class CRMContact:
    """CRM联系人数据模型。"""
    id: str
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    position: Optional[str] = None
    source: str = "crm"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CRMDeal:
    """CRM商机数据模型。"""
    id: str
    name: str
    stage: str = "prospecting"
    amount: float = 0.0
    contact_id: Optional[str] = None
    probability: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class CRMAdapter(ExternalServiceAdapter):
    """CRM适配器抽象基类。"""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self._base_url = config.get("base_url", "")
        self._api_key = config.get("api_key", "")
        self._client_id = config.get("client_id", "")
        self._client_secret = config.get("client_secret", "")

    @property
    def service_name(self) -> str:
        return "crm"

    async def call(self, request: ServiceRequest) -> ServiceResponse:
        """调用CRM API。"""
        raise NotImplementedError

    async def health_check(self) -> bool:
        """CRM系统健康检查。"""
        return True

    async def query_contacts(
        self,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> List[CRMContact]:
        """查询联系人。"""
        raise NotImplementedError

    async def get_contact(self, contact_id: str) -> Optional[CRMContact]:
        """获取联系人详情。"""
        raise NotImplementedError

    async def sync_contacts(
        self,
        contacts: List[CRMContact],
        sync_type: str = "incremental",
    ) -> Dict[str, Any]:
        """同步联系人到CRM。"""
        raise NotImplementedError


class SalesforceAdapter(CRMAdapter):
    """Salesforce CRM适配器。"""

    @property
    def service_name(self) -> str:
        return "salesforce"

    async def call(self, request: ServiceRequest) -> ServiceResponse:
        logger.info(
            f"Salesforce API调用: {request.method} {request.path}"
        )
        return ServiceResponse(
            success=True,
            data={"message": f"模拟Salesforce响应: {request.path}"},
        )

    async def query_contacts(
        self,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> List[CRMContact]:
        logger.info(f"Salesforce查询联系人: query={query}, limit={limit}")
        return [
            CRMContact(
                id=f"sf_{i}",
                name=f"联系人{i}",
                email=f"contact{i}@example.com",
                source="salesforce",
            )
            for i in range(min(limit, 3))
        ]

    async def get_contact(self, contact_id: str) -> Optional[CRMContact]:
        return CRMContact(
            id=contact_id,
            name="示例联系人",
            email="test@example.com",
            source="salesforce",
        )

    async def sync_contacts(
        self,
        contacts: List[CRMContact],
        sync_type: str = "incremental",
    ) -> Dict[str, Any]:
        return {
            "sync_id": "sync_001",
            "status": "completed",
            "synced_count": len(contacts),
        }


class FenxiangAdapter(CRMAdapter):
    """纷享销客CRM适配器。"""

    @property
    def service_name(self) -> str:
        return "fenxiang"

    async def call(self, request: ServiceRequest) -> ServiceResponse:
        return ServiceResponse(
            success=True,
            data={"message": f"模拟纷享销客响应: {request.path}"},
        )

    async def query_contacts(
        self,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> List[CRMContact]:
        return [
            CRMContact(
                id=f"fx_{i}",
                name=f"客户{i}",
                company="示例公司",
                source="fenxiang",
            )
            for i in range(min(limit, 2))
        ]

    async def get_contact(self, contact_id: str) -> Optional[CRMContact]:
        return CRMContact(
            id=contact_id, name="纷享客户", company="纷享科技", source="fenxiang"
        )

    async def sync_contacts(
        self,
        contacts: List[CRMContact],
        sync_type: str = "incremental",
    ) -> Dict[str, Any]:
        return {
            "sync_id": "fx_sync_001",
            "status": "completed",
            "synced_count": len(contacts),
        }


class CRMAdapterFactory:
    """CRM适配器工厂。

    根据类型创建对应的CRM适配器实例。
    """

    _adapters: Dict[str, Type[CRMAdapter]] = {
        "salesforce": SalesforceAdapter,
        "fenxiang": FenxiangAdapter,
    }

    @classmethod
    def create_adapter(
        cls,
        adapter_type: str,
        config: Dict[str, Any],
    ) -> CRMAdapter:
        """创建CRM适配器。

        Args:
            adapter_type: 适配器类型（salesforce/fenxiang）。
            config: 配置字典。

        Returns:
            CRM适配器实例。

        Raises:
            ValueError: 未知的适配器类型。
        """
        adapter_class = cls._adapters.get(adapter_type)
        if adapter_class is None:
            raise ValueError(
                f"未知的CRM适配器类型: {adapter_type}，"
                f"支持: {', '.join(cls._adapters.keys())}"
            )
        return adapter_class(config)

    @classmethod
    def register_adapter(
        cls,
        adapter_type: str,
        adapter_class: Type[CRMAdapter],
    ) -> None:
        """注册自定义CRM适配器。

        Args:
            adapter_type: 适配器类型标识。
            adapter_class: 适配器类。
        """
        cls._adapters[adapter_type] = adapter_class

    @classmethod
    def list_supported_types(cls) -> List[str]:
        """列出支持的CRM类型。"""
        return list(cls._adapters.keys())
