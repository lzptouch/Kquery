"""缓存预热机制。

系统启动时预加载热点数据到多级缓存，降低初始请求延迟。
支持配置化预热策略和可扩展的预热数据源。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from app.core.cache import MultiLevelCache, ml_cache, redis_cache_service
from app.core.logger import get_logger
from app.core.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


class CacheWarmer:
    """缓存预热器。

    在应用启动时自动加载热点数据到缓存，减少首次请求延迟。
    支持多种预热策略和优先级控制。

    Attributes:
        cache: 多级缓存实例
        preheat_tasks: 预热任务列表
        is_warmed: 是否已完成预热

    使用示例:
        warmer = CacheWarmer()

        # 注册预热任务
        warmer.register("config", load_config, priority=1, l1_ttl=300, l2_ttl=3600)
        warmer.register("user_roles", load_roles, priority=2)

        # 执行预热
        await warmer.warm_all()
    """

    def __init__(
        self,
        cache: Optional[MultiLevelCache] = None,
        enabled: bool = True,
        timeout_seconds: float = 30.0,
    ):
        """初始化缓存预热器。

        Args:
            cache: 多级缓存实例（默认使用全局实例）
            enabled: 是否启用预热
            timeout_seconds: 单个预热任务超时时间
        """
        self.cache = cache or ml_cache
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds
        self.is_warmed = False
        self._tasks: List[Dict[str, Any]] = []
        self._stats = {
            "total_tasks": 0,
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "elapsed_ms": 0.0,
        }

    def register(
        self,
        name: str,
        loader: Callable[[], Awaitable[Any]],
        key: Optional[str] = None,
        priority: int = 10,
        l1_ttl: int = 60,
        l2_ttl: int = 1800,
        condition: Optional[Callable[[], bool]] = None,
    ) -> None:
        """注册预热任务。

        Args:
            name: 任务名称（用于日志和统计）
            loader: 异步数据加载函数
            key: 缓存键（默认使用 name）
            priority: 优先级（数字越小优先级越高，1-100）
            l1_ttl: L1 缓存 TTL
            l2_ttl: L2 缓存 TTL
            condition: 可选的执行条件函数
        """
        self._tasks.append({
            "name": name,
            "loader": loader,
            "key": key or name,
            "priority": priority,
            "l1_ttl": l1_ttl,
            "l2_ttl": l2_ttl,
            "condition": condition,
        })
        self._stats["total_tasks"] += 1
        logger.debug(f"注册缓存预热任务: {name} (priority={priority})")

    async def _run_task(self, task: Dict[str, Any]) -> bool:
        """执行单个预热任务。

        Returns:
            是否成功
        """
        name = task["name"]
        key = task["key"]

        # 检查条件
        if task["condition"] and not task["condition"]():
            logger.debug(f"预热任务跳过: {name} (condition=False)")
            self._stats["skipped"] += 1
            return True

        # 检查是否已缓存
        try:
            existing = await self.cache.get(key)
            if existing is not None:
                logger.debug(f"预热任务跳过: {name} (已缓存)")
                self._stats["skipped"] += 1
                return True
        except Exception as e:
            logger.warning(f"预热检查缓存失败: {name} - {e}")

        # 加载数据
        try:
            start = time.time()
            data = await asyncio.wait_for(
                task["loader"](),
                timeout=self.timeout_seconds,
            )
            elapsed = (time.time() - start) * 1000

            # 写入缓存
            await self.cache.set(
                key,
                data,
                l1_ttl=task["l1_ttl"],
                l2_ttl=task["l2_ttl"],
            )

            logger.info(
                f"缓存预热成功: {name} "
                f"| key={key} "
                f"| size={len(str(data)) if data else 0} "
                f"| time={elapsed:.1f}ms"
            )
            self._stats["success"] += 1
            return True

        except asyncio.TimeoutError:
            logger.warning(f"缓存预热超时: {name} (>{self.timeout_seconds}s)")
            self._stats["failed"] += 1
            return False
        except Exception as e:
            logger.error(f"缓存预热失败: {name} - {e}")
            self._stats["failed"] += 1
            return False

    async def warm_all(self, concurrent_limit: int = 3) -> Dict[str, Any]:
        """执行所有预热任务。

        按优先级排序，同一优先级可并发执行。

        Args:
            concurrent_limit: 同一优先级最大并发数

        Returns:
            预热统计信息
        """
        if not self.enabled:
            logger.info("缓存预热已禁用")
            return {**self._stats, "enabled": False}

        if self.is_warmed:
            logger.info("缓存已预热，跳过")
            return {**self._stats, "enabled": True}

        start_time = time.time()
        logger.info("=" * 40)
        logger.info("开始缓存预热...")

        # 按优先级排序
        sorted_tasks = sorted(self._tasks, key=lambda t: t["priority"])

        # 按优先级分组执行
        current_priority = None
        priority_group: List[Dict[str, Any]] = []

        for task in sorted_tasks:
            if current_priority is not None and task["priority"] != current_priority:
                # 执行前一组
                await self._run_group(priority_group, concurrent_limit)
                priority_group = []

            current_priority = task["priority"]
            priority_group.append(task)

        # 执行最后一组
        if priority_group:
            await self._run_group(priority_group, concurrent_limit)

        self._stats["elapsed_ms"] = (time.time() - start_time) * 1000
        self.is_warmed = True

        logger.info("=" * 40)
        logger.info(
            f"缓存预热完成 | "
            f"成功={self._stats['success']} "
            f"失败={self._stats['failed']} "
            f"跳过={self._stats['skipped']} "
            f"耗时={self._stats['elapsed_ms']:.1f}ms"
        )

        return {**self._stats, "enabled": True}

    async def _run_group(
        self,
        tasks: List[Dict[str, Any]],
        concurrent_limit: int,
    ) -> None:
        """并发执行一组任务（带限流）。"""
        semaphore = asyncio.Semaphore(concurrent_limit)

        async def _run_with_limit(task: Dict[str, Any]) -> bool:
            async with semaphore:
                return await self._run_task(task)

        await asyncio.gather(*[_run_with_limit(t) for t in tasks], return_exceptions=True)

    def get_stats(self) -> Dict[str, Any]:
        """获取预热统计。"""
        return {**self._stats, "is_warmed": self.is_warmed, "enabled": self.enabled}

    async def invalidate(self, pattern: Optional[str] = None) -> None:
        """使预热缓存失效。

        Args:
            pattern: 匹配模式，None 则使所有预热 key 失效
        """
        if pattern:
            await self.cache.invalidate_pattern(pattern)
            logger.info(f"缓存预热数据失效: pattern={pattern}")
        else:
            for task in self._tasks:
                await self.cache.invalidate(task["key"])
            logger.info("所有缓存预热数据已失效")

        self.is_warmed = False


# ==================== 全局预热器实例 ====================

cache_warmer = CacheWarmer()


# ==================== 内置预热任务加载器 ====================

async def _warm_system_config() -> Dict[str, Any]:
    """加载系统配置到缓存。"""
    s = get_settings()
    return {
        "app_name": s.app_name,
        "environment": s.environment.value,
        "debug": s.debug,
        "version": "2.0.0",
        "features": {
            "rag": True,
            "graphrag": True,
            "knowledge_graph": True,
            "multimodal": True,
        },
    }


async def _warm_rag_config() -> Dict[str, Any]:
    """加载 RAG 配置到缓存。"""
    s = get_settings()
    return {
        "chunk_size": s.chunk_size,
        "chunk_overlap": s.chunk_overlap,
        "top_k": s.rag_top_k,
        "initial_top_k": s.rag_initial_top_k,
        "use_hyde": s.rag_use_hyde,
        "use_rrf": s.rag_use_rrf,
        "use_reranker": s.rag_use_reranker,
    }


async def _warm_security_config() -> Dict[str, Any]:
    """加载安全配置到缓存。"""
    s = get_settings()
    return {
        "rate_limit_requests": s.rate_limit_requests,
        "rate_limit_window": s.rate_limit_window_seconds,
        "max_file_size_mb": s.max_file_size_mb,
        "allowed_extensions": list(s.allowed_extensions),
        "jwt_expiration_hours": s.jwt_expiration_hours,
    }


async def _warm_entity_types() -> List[str]:
    """加载知识图谱实体类型列表。"""
    try:
        from app.repositories.graph.schema import EntityType
        return [e.value for e in EntityType]
    except Exception:
        return []


async def _warm_relation_types() -> List[str]:
    """加载知识图谱关系类型列表。"""
    try:
        from app.repositories.graph.schema import RelationshipType
        return [r.value for r in RelationshipType]
    except Exception:
        return []


def register_default_warmers() -> None:
    """注册默认的内置预热任务。"""
    cache_warmer.register(
        "system_config",
        _warm_system_config,
        key="cache:system:config",
        priority=1,
        l1_ttl=300,
        l2_ttl=3600,
    )
    cache_warmer.register(
        "rag_config",
        _warm_rag_config,
        key="cache:rag:config",
        priority=2,
        l1_ttl=300,
        l2_ttl=3600,
    )
    cache_warmer.register(
        "security_config",
        _warm_security_config,
        key="cache:security:config",
        priority=2,
        l1_ttl=300,
        l2_ttl=1800,
    )
    cache_warmer.register(
        "entity_types",
        _warm_entity_types,
        key="cache:graph:entity_types",
        priority=5,
        l1_ttl=600,
        l2_ttl=7200,
    )
    cache_warmer.register(
        "relation_types",
        _warm_relation_types,
        key="cache:graph:relation_types",
        priority=5,
        l1_ttl=600,
        l2_ttl=7200,
    )
    logger.info(f"已注册 {len(cache_warmer._tasks)} 个默认缓存预热任务")


# 模块加载时自动注册默认预热器
register_default_warmers()
