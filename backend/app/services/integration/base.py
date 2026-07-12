"""外部服务适配器抽象基类。

定义外部服务集成的统一接口，所有适配器需实现此接口。
归属于L6连接集成层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ServiceRequest:
    """外部服务请求。

    Attributes:
        method: HTTP方法。
        path: 请求路径。
        body: 请求体。
        headers: 请求头。
        params: 查询参数。
    """
    method: str = "GET"
    path: str = ""
    body: Optional[Dict[str, Any]] = None
    headers: Dict[str, str] = field(default_factory=dict)
    params: Dict[str, str] = field(default_factory=dict)


@dataclass
class ServiceResponse:
    """外部服务响应。

    Attributes:
        success: 是否成功。
        status_code: HTTP状态码。
        data: 响应数据。
        error: 错误信息。
    """
    success: bool = True
    status_code: int = 200
    data: Optional[Any] = None
    error: Optional[str] = None


class ExternalServiceAdapter(ABC):
    """外部服务适配器抽象基类。

    所有外部系统适配器必须实现此接口。
    """

    @abstractmethod
    async def call(self, request: ServiceRequest) -> ServiceResponse:
        """调用外部服务。

        Args:
            request: 服务请求。

        Returns:
            服务响应。
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查。

        Returns:
            服务是否可用。
        """
        ...

    @property
    @abstractmethod
    def service_name(self) -> str:
        """服务名称。"""
        ...
