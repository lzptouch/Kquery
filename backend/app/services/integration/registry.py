"""外部服务注册中心。

管理所有外部服务适配器的注册、发现和健康检查。
归属于L6连接集成层。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from app.core.logger import get_logger
from app.services.integration.base import ExternalServiceAdapter, ServiceResponse

logger = get_logger(__name__)


class ExternalServiceRegistry:
    """外部服务注册中心。

    管理所有已注册的适配器，提供统一的发现和健康检查接口。
    """

    def __init__(self) -> None:
        self._adapters: Dict[str, ExternalServiceAdapter] = {}

    def register(self, name: str, adapter: ExternalServiceAdapter) -> None:
        """注册外部服务适配器。

        Args:
            name: 服务名称。
            adapter: 适配器实例。
        """
        self._adapters[name] = adapter
        logger.info(f"外部服务已注册: {name}")

    def unregister(self, name: str) -> None:
        """注销外部服务适配器。

        Args:
            name: 服务名称。
        """
        self._adapters.pop(name, None)
        logger.info(f"外部服务已注销: {name}")

    def get(self, name: str) -> ExternalServiceAdapter:
        """获取适配器。

        Args:
            name: 服务名称。

        Returns:
            适配器实例。

        Raises:
            KeyError: 服务未注册。
        """
        if name not in self._adapters:
            raise KeyError(f"外部服务未注册: {name}")
        return self._adapters[name]

    def list_services(self) -> List[Dict[str, str]]:
        """列出所有已注册服务。

        Returns:
            服务信息列表。
        """
        return [
            {"name": name, "type": type(adapter).__name__}
            for name, adapter in self._adapters.items()
        ]

    async def health_check_all(self) -> Dict[str, bool]:
        """批量健康检查。

        Returns:
            服务名称到健康状态的映射。
        """
        results: Dict[str, bool] = {}
        for name, adapter in self._adapters.items():
            try:
                results[name] = await adapter.health_check()
            except Exception as e:
                logger.warning(f"外部服务健康检查失败: {name}, error={e}")
                results[name] = False
        return results

    async def call_service(
        self,
        name: str,
        method: str = "GET",
        path: str = "",
        **kwargs,
    ) -> ServiceResponse:
        """调用外部服务。

        Args:
            name: 服务名称。
            method: HTTP方法。
            path: 请求路径。
            **kwargs: 传递给ServiceRequest的额外参数。

        Returns:
            服务响应。
        """
        from app.services.integration.base import ServiceRequest

        adapter = self.get(name)
        request = ServiceRequest(method=method, path=path, **kwargs)
        return await adapter.call(request)

    @property
    def service_count(self) -> int:
        """已注册服务数量。"""
        return len(self._adapters)


# 全局单例
external_service_registry = ExternalServiceRegistry()
