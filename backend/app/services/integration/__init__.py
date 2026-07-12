"""连接集成层。

提供外部系统集成适配器，包括CRM、订单系统、企业微信等。
归属于L6连接集成层。
"""

from app.services.integration.registry import external_service_registry
from app.services.integration.crm_adapter import CRMAdapter, SalesforceAdapter, FenxiangAdapter
from app.services.integration.order_sdk import OrderSDK
from app.services.integration.base import ExternalServiceAdapter

__all__ = [
    "external_service_registry",
    "CRMAdapter",
    "SalesforceAdapter",
    "FenxiangAdapter",
    "OrderSDK",
    "ExternalServiceAdapter",
]
