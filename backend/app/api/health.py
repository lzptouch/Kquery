"""KQuery Pro 健康检查接口。

提供系统健康状态监控，用于：
- 负载均衡器探活检查
- Kubernetes liveness/readiness 探针
- 运维监控和告警
- 依赖服务状态检测

端点说明：
- GET /health: 基本健康检查（应用是否存活）
- GET /health/ready: 就绪检查（依赖服务是否可用）
- GET /health/live: 存活检查（应用进程是否正常）
- GET /health/详细: 完整健康信息（包含所有组件状态）

使用示例：
    # 基本健康检查
    curl http://localhost:8000/health
    # {"status": "healthy", "timestamp": "2026-04-11T..."}
    
    # 就绪检查（包含依赖服务状态）
    curl http://localhost:8000/health/ready
    # {
    #   "status": "ready",
    #   "components": {
    #     "milvus": "healthy",
    #     "redis": "healthy"
    #   }
    # }
"""

from __future__ import annotations

import asyncio
import platform
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

from app.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["Health"])


class HealthStatus(str, Enum):
    """健康状态枚举。
    
    Attributes:
        HEALTHY: 健康，所有功能正常
        DEGRADED: 降级，部分功能受限但可服务
        UNHEALTHY: 不健康，关键服务不可用
    """
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ComponentStatus:
    """组件状态信息。
    
    Attributes:
        name: 组件名称
        status: 组件状态 (healthy/unhealthy)
        response_time_ms: 响应耗时(毫秒)
        message: 状态描述或错误信息
        last_check: 最后检查时间
        metadata: 附加元数据
    """
    
    def __init__(
        self,
        name: str,
        status: str = "healthy",
        response_time_ms: Optional[float] = None,
        message: Optional[str] = None,
        **metadata: Any,
    ) -> None:
        """初始化组件状态。
        
        Args:
            name: 组件名称
            status: 状态字符串
            response_time_ms: 响应耗时(ms)
            message: 描述信息
            **metadata: 附加元数据
        """
        self.name = name
        self.status = status
        self.response_time_ms = response_time_ms
        self.message = message
        self.last_check = datetime.now(timezone.utc).isoformat()
        self.metadata = metadata
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式。"""
        result: Dict[str, Any] = {
            "status": self.status,
            "last_check": self.last_check,
        }
        
        if self.response_time_ms is not None:
            result["response_time_ms"] = round(self.response_time_ms, 2)
        if self.message:
            result["message"] = self.message
        if self.metadata:
            result.update(self.metadata)
        
        return result


async def check_milvus() -> ComponentStatus:
    """检查Milvus连接状态。
    
    Returns:
        Milvus组件状态对象
    """
    start_time = time.perf_counter()
    
    try:
        from app.repositories.database.milvus_connector import MilvusConnector
        
        connector = MilvusConnector()
        # 执行简单查询测试连接（list_collections 是同步方法）
        connector.list_collections()
        
        duration_ms = (time.perf_counter() - start_time) * 1000
        
        return ComponentStatus(
            name="milvus",
            status="healthy",
            response_time_ms=duration_ms,
            message="Connection successful",
        )
        
    except Exception as exc:
        logger.warning(f"Milvus health check failed: {exc}")
        return ComponentStatus(
            name="milvus",
            status="unhealthy",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            message=f"Connection failed: {str(exc)[:100]}",
        )


async def check_redis() -> ComponentStatus:
    """检查Redis连接状态。
    
    Returns:
        Redis组件状态对象
    """
    start_time = time.perf_counter()
    
    try:
        import redis.asyncio as aioredis
        
        from app.core.settings import get_settings
        settings = get_settings()
        
        client = aioredis.from_url(
            settings.redis_url,
            max_connections=1,
            decode_responses=True,
        )
        
        await client.ping()
        await client.close()
        
        duration_ms = (time.perf_counter() - start_time) * 1000
        
        return ComponentStatus(
            name="redis",
            status="healthy",
            response_time_ms=duration_ms,
            message="Connection successful",
        )
        
    except Exception as exc:
        logger.warning(f"Redis health check failed: {exc}")
        return ComponentStatus(
            name="redis",
            status="unhealthy",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            message=f"Connection failed: {str(exc)[:100]}",
        )


async def check_neo4j() -> ComponentStatus:
    """检查Neo4j连接状态（知识图谱）。
    
    Returns:
        Neo4j组件状态对象
    """
    start_time = time.perf_counter()
    
    try:
        from app.repositories.graph.neo4j_connector import neo4j_manager
        
        connector = neo4j_manager.connector
        # 若启动时尚未建立连接，则先建立连接
        if not connector._is_connected:
            await connector.connect()
        # 执行简单Cypher查询测试连接
        await connector.execute_query("RETURN 1 AS test")
        
        duration_ms = (time.perf_counter() - start_time) * 1000
        
        return ComponentStatus(
            name="neo4j",
            status="healthy",
            response_time_ms=duration_ms,
            message="Connection successful (Knowledge Graph)",
        )
        
    except ImportError:
        # Neo4j未安装或配置
        return ComponentStatus(
            name="neo4j",
            status="not_configured",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            message="Neo4j not configured or installed",
        )
    except Exception as exc:
        logger.warning(f"Neo4j health check failed: {exc}")
        return ComponentStatus(
            name="neo4j",
            status="unhealthy",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            message=f"Connection failed: {str(exc)[:100]}",
        )


@router.get("/health")
async def health_check(request: Request) -> Dict[str, Any]:
    """基本健康检查。
    
    仅返回应用自身状态，不检查依赖服务。
    用于负载均衡器和Kubernetes的liveness探针。
    
    Returns:
        健康状态字典
    """
    return {
        "status": HealthStatus.HEALTHY.value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": request.app.version if hasattr(request.app, "version") else "1.0.0",
    }


@router.get("/health/ready")
async def readiness_check(request: Request) -> Dict[str, Any]:
    """就绪检查。
    
    检查所有关键依赖服务是否可用，
    如果任何核心服务不可用则返回 degraded 或 unhealthy。
    用于Kubernetes的readiness探针。
    
    Returns:
        就绪状态及各组件状态详情
    """
    # 并行检查所有服务
    milvus_status, redis_status, neo4j_status = await asyncio.gather(
        check_milvus(),
        check_redis(),
        check_neo4j(),
        return_exceptions=True,
    )
    
    # 汇总结果
    components = {
        "milvus": milvus_status.to_dict(),
        "redis": redis_status.to_dict(),
        "neo4j": neo4j_status.to_dict(),
    }
    
    # 判断整体状态
    core_services = [milvus_status, redis_status]
    if any(s.status == "unhealthy" for s in core_services):
        overall_status = HealthStatus.UNHEALTHY
    elif any(s.status in ("unhealthy", "not_configured") for s in [neo4j_status]):
        # Neo4j不可用是降级（不影响基础RAG功能）
        overall_status = HealthStatus.DEGRADED
    else:
        overall_status = HealthStatus.HEALTHY
    
    return {
        "status": overall_status.value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": components,
    }


@router.get("/health/live")
async def liveness_check(request: Request) -> Dict[str, Any]:
    """存活检查。
    
    验证应用进程是否正常运行且未死锁。
    轻量级检查，用于快速判断应用是否需要重启。
    
    Returns:
        存活状态
    """
    # 这里可以添加更复杂的存活检查逻辑
    # 例如：检查主线程是否响应、是否有死锁等
    
    return {
        "status": HealthStatus.HEALTHY.value,
        "alive": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - getattr(request.app.state, "start_time", time.time())),
    }


@router.get("/health/detailed")
async def detailed_health_check(request: Request) -> Dict[str, Any]:
    """详细健康检查。
    
    返回完整的系统健康信息，包括：
    - 应用基本信息
    - 各组件详细状态
    - 系统资源使用情况
    - 配置摘要（脱敏）
    
    注意：此接口包含较多信息，不建议频繁调用。
    
    Returns:
        完整的健康报告
    """
    # 收集组件状态
    readiness = await readiness_check(request)
    
    # 系统信息
    system_info = {
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "process_id": __import__("os").getpid(),
        "hostname": platform.node(),
    }
    
    return {
        **readiness,
        "system_info": system_info,
        "configuration_summary": {
            "environment": getattr(
                getattr(request.app, "state", None), 
                "environment", 
                "unknown"
            ),
            "features": {
                "multimodal": False,  # 音频视频多模态功能已移除
                "knowledge_graph": readiness["components"]["neo4j"]["status"] == "healthy",
            },
        },
    }
