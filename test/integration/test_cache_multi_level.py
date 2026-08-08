from unittest.mock import AsyncMock

import pytest


@pytest.mark.integration
class TestCacheMultiLevel:
    async def test_l1_and_l2_consistency(self, test_env: dict) -> None:
        import redis.asyncio as redis

        from app.core.cache import MemoryCache, MultiLevelCache, RedisCacheService

        l1 = MemoryCache(maxsize=100, default_ttl=60)
        redis_client = redis.from_url(test_env["REDIS_URL"])
        l2 = RedisCacheService(redis_client=redis_client)
        await l2.init()
        cache = MultiLevelCache(memory_cache=l1, redis_cache=l2)

        await cache.set("key", "value")
        # L1 命中
        v1 = await cache.get("key")
        assert v1 == "value"
        # L1 失效后应命中 L2
        await l1.delete("key")
        v2 = await cache.get("key")
        assert v2 == "value"

    async def test_cache_warmer(self) -> None:
        from app.core.cache_warmer import CacheWarmer
        from app.core.cache import ml_cache

        warmer = CacheWarmer(cache=ml_cache)
        loader = AsyncMock(return_value="loaded")
        warmer.register("test-task", loader, priority=1)
        await warmer.warm_all()
        assert warmer.is_warmed is True
        assert warmer._stats["success"] >= 1
        loader.assert_awaited_once()
