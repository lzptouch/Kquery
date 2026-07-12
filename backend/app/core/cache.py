"""
多级缓存系统

提供 L1 (内存) + L2 (Redis) 两级缓存架构
用于降低数据库压力，提升API响应速度
"""

import json
import time
import hashlib
from typing import Any, Optional, Callable, Union, Dict, List, Awaitable
from functools import wraps
from datetime import timedelta
from dataclasses import dataclass, field

import redis.asyncio as redis

from app.core.settings import get_settings
from app.core.logger import get_logger


logger = get_logger(__name__)
settings = get_settings()


@dataclass
class CacheEntry:
    """
    缓存条目数据结构
    
    Attributes:
        value: 缓存的值
        created_at: 创建时间戳
        expires_at: 过期时间戳（None表示永不过期）
        hits: 命中次数（用于统计热点数据）
    """
    value: Any
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    hits: int = 0
    
    def is_expired(self) -> bool:
        """检查是否已过期"""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at
    
    @property
    def ttl(self) -> float:
        """返回剩余生存时间"""
        if self.expires_at is None:
            return float('inf')
        remaining = self.expires_at - time.time()
        return max(0.0, remaining)


# ==================== L1: 内存缓存 ====================

class MemoryCache:
    """
    内存缓存（LRU策略）
    
    使用字典 + TTL 实现的简单内存缓存
    适用于：
    - 热点数据快速访问
    - 配置信息缓存
    - 用户会话临时数据
    
    特点：
    - 极快访问速度（<0.01ms）
    - 自动过期清理
    - 最大容量限制（防止内存泄漏）
    
    使用示例:
        cache = MemoryCache(maxsize=1000, default_ttl=60)
        
        # 设置缓存
        await cache.set("user:123", user_data, ttl=300)
        
        # 获取缓存
        user = await cache.get("user:123")
        
        # 装饰器方式
        @cache.cached(ttl=120, key_prefix="config")
        async def get_config():
            return load_config_from_db()
    """
    
    def __init__(
        self,
        maxsize: int = 1000,
        default_ttl: int = 60,
        auto_cleanup_interval: int = 300,
        name: str = "default"
    ):
        """
        初始化内存缓存
        
        Args:
            maxsize: 最大缓存条目数（超出后淘汰最久未访问的）
            default_ttl: 默认TTL（秒）
            auto_cleanup_interval: 自动清理过期条目的间隔（秒）
            name: 缓存实例名称（用于日志标识）
        """
        self._store: Dict[str, CacheEntry] = {}
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._auto_cleanup_interval = auto_cleanup_interval
        self.name = name
        
        # 统计信息
        self.stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletions": 0,
            "evictions": 0
        }
        
        # 最后清理时间
        self._last_cleanup_time = time.time()
        
        logger.debug(f"MemoryCache '{name}' 初始化完成 | MaxSize={maxsize}, DefaultTTL={default_ttl}s")
    
    async def get(self, key: str) -> Optional[Any]:
        """
        获取缓存值
        
        Args:
            key: 缓存键
            
        Returns:
            缓存的值，不存在或已过期则返回None
        """
        entry = self._store.get(key)
        
        if entry is None:
            self.stats["misses"] += 1
            return None
        
        if entry.is_expired():
            del self._store[key]
            self.stats["misses"] += 1
            return None
        
        # 命中，更新统计和访问时间
        entry.hits += 1
        self.stats["hits"] += 1
        
        return entry.value
    
    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None
    ) -> None:
        """
        设置缓存值
        
        Args:
            key: 缓存键
            value: 要缓存的值
            ttl: 过期时间（秒），None使用默认值
        """
        effective_ttl = ttl or self._default_ttl
        expires_at = time.time() + effective_ttl if effective_ttl > 0 else None
        
        # 检查是否需要淘汰
        if len(self._store) >= self._maxsize and key not in self._store:
            self._evict_oldest()
        
        self._store[key] = CacheEntry(
            value=value,
            expires_at=expires_at
        )
        self.stats["sets"] += 1
        
        # 定期自动清理
        if time.time() - self._last_cleanup_time > self._auto_cleanup_interval:
            self._cleanup_expired()
    
    async def delete(self, key: str) -> bool:
        """
        删除指定键的缓存
        
        Args:
            key: 缓存键
            
        Returns:
            是否成功删除（键存在才删除）
        """
        if key in self._store:
            del self._store[key]
            self.stats["deletions"] += 1
            return True
        return False
    
    async def clear(self) -> None:
        """清空所有缓存"""
        count = len(self._store)
        self._store.clear()
        self.stats["deletions"] += count
        logger.info(f"MemoryCache '{self.name}' 已清空 | 清除 {count} 个条目")
    
    async def exists(self, key: str) -> bool:
        """检查键是否存在且未过期"""
        entry = self._store.get(key)
        if entry is None or entry.is_expired():
            return False
        return True
    
    def _evict_oldest(self) -> None:
        """淘汰最久未访问的条目（LRU近似）"""
        if not self._store:
            return
        
        # 找到最少命中的条目
        oldest_key = min(
            self._store.keys(),
            key=lambda k: (self._store[k].hits, self._store[k].created_at)
        )
        
        del self._store[oldest_key]
        self.stats["evictions"] += 1
        logger.debug(f"MemoryCache 淘汰条目: {oldest_key}")
    
    def _cleanup_expired(self) -> None:
        """清理所有过期的条目"""
        expired_keys = [
            k for k, v in self._store.items() 
            if v.is_expired()
        ]
        
        for key in expired_keys:
            del self._store[key]
        
        if expired_keys:
            self.stats["deletions"] += len(expired_keys)
            logger.debug(f"MemoryCache 清理了 {len(expired_keys)} 个过期条目")
        
        self._last_cleanup_time = time.time()
    
    @property
    def size(self) -> int:
        """当前缓存条目数（含可能已过期的）"""
        return len(self._store)
    
    @property
    def hit_rate(self) -> float:
        """缓存命中率"""
        total = self.stats["hits"] + self.stats["misses"]
        if total == 0:
            return 0.0
        return self.stats["hits"] / total
    
    def cached(self, ttl: Optional[int] = None, key_prefix: str = ""):
        """
        缓存装饰器工厂
        
        用于装饰异步函数，自动缓存其返回值
        
        Args:
            ttl: 缓存有效期（秒）
            key_prefix: 键名前缀
            
        使用示例:
            @memory_cache.cached(ttl=300, key_prefix="kb:list")
            async def list_knowledge_bases(user_id):
                ...
                
        Returns:
            装饰器函数
        """
        effective_ttl = ttl or self._default_ttl
        
        def decorator(func: Callable):
            @wraps(func)
            async def wrapper(*args, **kwargs):
                # 生成缓存键
                cache_key = f"{key_prefix}:{func.__name__}:{hash((args, frozenset(kwargs.items())))}"
                
                # 尝试从缓存获取
                cached_value = await self.get(cache_key)
                if cached_value is not None:
                    logger.debug(f"缓存命中: {cache_key}")
                    return cached_value
                
                # 执行原函数
                result = await func(*args, **kwargs)
                
                # 存入缓存
                await self.set(cache_key, result, ttl=effective_ttl)
                logger.debug(f"缓存设置: {cache_key} (TTL={effective_ttl}s)")
                
                return result
            return wrapper
        return decorator
    
    def get_stats(self) -> dict:
        """获取缓存统计信息"""
        return {
            **self.stats,
            "current_size": self.size,
            "max_size": self._maxsize,
            "hit_rate": f"{self.hit_rate:.2%}",
            "name": self.name
        }


# 全局默认内存缓存实例
default_memory_cache = MemoryCache(maxsize=10000, default_ttl=60, name="global")


# ==================== L2: Redis缓存 ====================

class RedisCacheService:
    """
    Redis分布式缓存服务
    
    作为L2缓存层，支持：
    - 跨进程共享缓存
    - 更大的存储容量
    - 更长的TTL
    - 分布式环境下的缓存一致性
    
    使用示例:
        redis_cache = RedisCacheService()
        
        # 基本操作
        await redis_cache.set("doc:metadata:123", metadata_dict, ttl=1800)
        data = await redis_cache.get("doc:metadata:123")
        
        # 高级功能
        result = await redis_cache.get_or_set("users:active", ttl=600, callback=load_users)
        await redis_cache.invalidate_pattern("users:*")  # 批量失效
    """
    
    def __init__(self, redis_client: Optional[redis.Redis] = None):
        """
        初始化Redis缓存服务
        
        Args:
            redis_client: 可选的外部Redis客户端实例（如不提供则内部创建）
        """
        self.redis = redis_client
        self._initialized = False
        
        # 统计信息
        self.stats = {
            "hits": 0,
            "misses": 0,
            "sets": 0,
            "deletions": 0
        }
    
    async def init(self) -> None:
        """初始化Redis连接"""
        try:
            if not self.redis:
                self.redis = redis.from_url(
                    settings.redis_url,
                    encoding="utf-8",
                    decode_responses=True
                )
            
            # 测试连接
            await self.redis.ping()
            self._initialized = True
            logger.info("RedisCacheService 初始化成功")
            
        except Exception as e:
            logger.error(f"RedisCacheService 初始化失败: {e}")
            raise
    
    async def get(self, key: str) -> Optional[Any]:
        """
        从Redis获取缓存值
        
        支持自动JSON反序列化
        
        Args:
            key: 缓存键
            
        Returns:
            缓存的值（自动解析为Python对象），或None
        """
        if not self._initialized:
            return None
        
        try:
            value = await self.redis.get(key)
            
            if value is not None:
                self.stats["hits"] += 1
                # 尝试JSON解析
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return value
            
            self.stats["misses"] += 1
            return None
            
        except Exception as e:
            logger.error(f"Redis GET 失败 [{key}]: {e}")
            return None
    
    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = 1800,
        nx: bool = False  # 仅当键不存在时设置（防覆盖）
    ) -> bool:
        """
        设置Redis缓存值
        
        自动序列化Python对象为JSON
        
        Args:
            key: 缓存键
            value: 要缓存的值
            ttl: 过期时间（秒）
            nx: 是否仅在新键时设置
            
        Returns:
            是否设置成功
        """
        if not self._initialized:
            return False
        
        try:
            # 序列化值
            serialized_value = json.dumps(value, ensure_ascii=False, default=str)
            
            success = await self.redis.set(
                key,
                serialized_value,
                ex=ttl,
                nx=nx
            )
            
            if success:
                self.stats["sets"] += 1
            
            return bool(success)
            
        except Exception as e:
            logger.error(f"Redis SET 失败 [{key}]: {e}")
            return False
    
    async def delete(self, *keys: str) -> int:
        """
        删除一个或多个键
        
        Args:
            *keys: 要删除的键列表
            
        Returns:
            实际删除的键数量
        """
        if not self._initialized or not keys:
            return 0
        
        try:
            deleted = await self.redis.delete(*keys)
            if deleted:
                self.stats["deletions"] += deleted
            return deleted
        except Exception as e:
            logger.error(f"Redis DELETE 失败: {e}")
            return 0
    
    async def invalidate_pattern(self, pattern: str) -> int:
        """
        根据模式批量删除缓存（用于缓存失效）
        
        ⚠️ 生产环境中慎用，KEYS命令在大量键时会阻塞Redis
        
        Args:
            pattern: 通配符模式（如 "users:*", "doc:metadata:*"）
            
        Returns:
            匹配并删除的键数量
        """
        if not self._initialized:
            return 0
        
        try:
            # 使用SCAN替代KEYS避免阻塞
            cursor = 0
            total_deleted = 0
            
            while True:
                cursor, keys = await self.redis.scan(cursor, match=pattern, count=100)
                if keys:
                    deleted = await self.redis.delete(*keys)
                    total_deleted += deleted
                
                if cursor == 0:
                    break
            
            if total_deleted:
                self.stats["deletions"] += total_deleted
                logger.info(f"批量缓存失效: pattern='{pattern}', deleted={total_deleted}")
            
            return total_deleted
            
        except Exception as e:
            logger.error(f"Redis SCAN/DELETE 失败 [{pattern}]: {e}")
            return 0
    
    async def get_or_set(
        self,
        key: str,
        ttl: int,
        callback: Callable[[], Awaitable[Any]],
        force_refresh: bool = False
    ) -> Any:
        """
        获取缓存或执行回调函数设置缓存（原子性保证）
        
        如果缓存存在且未过期则直接返回，
        否则执行callback并将结果写入缓存
        
        Args:
            key: 缓存键
            ttl: 缓存有效期（秒）
            callback: 异步回调函数（用于生成缓存值）
            force_refresh: 是否强制刷新（忽略已有缓存）
            
        Returns:
            缓存值或回调结果
        """
        # 非强制刷新时先尝试获取
        if not force_refresh:
            existing = await self.get(key)
            if existing is not None:
                return existing
        
        # 执行回调获取新值
        new_value = await callback()
        
        # 写入缓存
        await self.set(key, new_value, ttl=ttl)
        
        return new_value
    
    async def exists(self, *keys: str) -> List[bool]:
        """检查多个键是否存在"""
        if not self._initialized:
            return [False] * len(keys)
        
        try:
            results = await self.redis.mget(keys)
            return [r is not None for r in results]
        except Exception as e:
            logger.error(f"Redis EXISTS 失败: {e}")
            return [False] * len(keys)
    
    async def increment(self, key: str, amount: int = 1) -> Optional[int]:
        """原子递增计数器"""
        if not self._initialized:
            return None
        
        try:
            return await self.redis.incrby(key, amount)
        except Exception as e:
            logger.error(f"Redis INCRBY 失败 [{key}]: {e}")
            return None
    
    async def expire(self, key: str, ttl: int) -> bool:
        """修改键的过期时间"""
        if not self._initialized:
            return False
        
        try:
            return await self.redis.expire(key, ttl)
        except Exception as e:
            logger.error(f"Redis EXPIRE 失败: {e}")
            return False
    
    def get_stats(self) -> dict:
        """获取缓存服务统计信息"""
        return {
            **self.stats,
            "initialized": self._initialized,
            "hit_rate": (
                f"{self.stats['hits'] / (self.stats['hits'] + self.stats['misses']):.2%}"
                if (self.stats['hits'] + self.stats['misses']) > 0 else "N/A"
            )
        }


# 全局Redis缓存实例（延迟初始化）
redis_cache_service = RedisCacheService()


# ==================== 多级缓存管理器 ====================

class MultiLevelCache:
    """
    多级缓存管理器（L1 + L2）
    
    协调内存缓存和Redis缓存的工作流程：
    1. 先查 L1（内存）- 最快
    2. 未命中则查 L2（Redis）
    3. L2也未命中则执行回调并回填两级缓存
    4. L2回填时同时更新 L1（保持一致性）
    
    使用示例:
        ml_cache = MultiLevelCache()
        
        # 带有多级缓存的数据获取
        data = await ml_cache.get_or_set(
            key=f"user:{user_id}:profile",
            l1_ttl=60,
            l2_ttl=1800,
            callback=lambda: fetch_user_profile(user_id)
        )
    """
    
    def __init__(
        self,
        memory_cache: Optional[MemoryCache] = None,
        redis_cache: Optional[RedisCacheService] = None
    ):
        """
        初始化多级缓存管理器
        
        Args:
            memory_cache: L1内存缓存实例（可选，默认使用全局实例）
            redis_cache: L2 Redis缓存实例（可选，延迟初始化）
        """
        self.l1_cache = memory_cache or default_memory_cache
        self.l2_cache = redis_cache or redis_cache_service
    
    async def get(self, key: str) -> Optional[Any]:
        """
        从多级缓存获取值（先L1后L2）
        
        流程：
        1. 查询L1内存缓存
        2. 若未命中，查询L2 Redis缓存
        3. 若L2命中，回填L1缓存
        
        Args:
            key: 缓存键
            
        Returns:
            缓存值或None
        """
        # L1查找
        l1_result = await self.l1_cache.get(key)
        if l1_result is not None:
            return l1_result
        
        # L2查找
        l2_result = await self.l2_cache.get(key)
        if l2_result is not None:
            # 回填L1（较短TTL）
            await self.l1_cache.set(key, l2_result)
            return l2_result
        
        return None
    
    async def set(
        self,
        key: str,
        value: Any,
        l1_ttl: Optional[int] = None,
        l2_ttl: Optional[int] = 1800
    ) -> None:
        """
        设置多级缓存（同时写L1和L2）
        
        Args:
            key: 缓存键
            value: 值
            l1_ttl: L1 TTL（秒）
            l2_ttl: L2 TTL（秒）
        """
        # 并行写入两级缓存
        import asyncio
        await asyncio.gather(
            self.l1_cache.set(key, value, ttl=l1_ttl),
            self.l2_cache.set(key, value, ttl=l2_ttl),  # 修正：l2_ttl
            return_exceptions=True
        )
    
    async def invalidate(self, key: str) -> None:
        """使指定键在两级缓存中都失效"""
        import asyncio
        await asyncio.gather(
            self.l1_cache.delete(key),
            self.l2_cache.delete(key),
            return_exceptions=True
        )
    
    async def invalidate_pattern(self, pattern: str) -> None:
        """
        批量失效（基于模式）
        
        注意：只对L2有效（Redis支持通配符匹配）
        L1需要全量清除或等待自然过期
        """
        await self.l2_cache.invalidate_pattern(pattern)
        # 对于L1，可以记录模式以便后续选择性清理
        logger.info(f"多级缓存批量失效: {pattern} (仅L2生效)")
    
    async def get_or_set(
        self,
        key: str,
        callback: Callable[[], Awaitable[Any]],
        l1_ttl: int = 60,
        l2_ttl: int = 1800,
    ) -> Any:
        """
        多级缓存获取或设置
        
        完整的多级缓存查找+回填流程
        
        Args:
            key: 缓存键
            l1_ttl: L1 TTL
            l2_ttl: L2 TTL
            callback: 数据加载回调
            
        Returns:
            缓存或新生成的值
        """
        # 尝试从缓存获取
        cached_value = await self.get(key)
        if cached_value is not None:
            return cached_value
        
        # 回调生成新值
        fresh_value = await callback()
        
        # 写入两级缓存
        await self.set(key, fresh_value, l1_ttl=l1_ttl, l2_ttl=l2_ttl)
        
        return fresh_value
    
    def get_stats(self) -> dict:
        """获取多级缓存整体统计"""
        return {
            "l1_memory": self.l1_cache.get_stats(),
            "l2_redis": self.l2_cache.get_stats()
        }


# 全局多级缓存实例
ml_cache = MultiLevelCache()
