import asyncio
from datetime import timedelta
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.cache import MemoryCache, MultiLevelCache


@pytest.mark.unit
class TestMemoryCache:
    async def test_set_and_get(self) -> None:
        cache = MemoryCache(maxsize=10, default_ttl=60)
        await cache.set("key", "value")
        assert await cache.get("key") == "value"

    async def test_ttl_expired(self) -> None:
        cache = MemoryCache(maxsize=10, default_ttl=0)
        await cache.set("key", "value", ttl=0.05)
        await asyncio.sleep(0.1)
        assert await cache.get("key") is None
        assert cache.stats["misses"] >= 1

    async def test_lru_eviction(self) -> None:
        cache = MemoryCache(maxsize=2, default_ttl=60)
        await cache.set("a", 1)
        await cache.set("b", 2)
        await cache.set("c", 3)
        assert await cache.get("a") is None
        assert await cache.get("b") == 2

    async def test_stats_hit(self) -> None:
        cache = MemoryCache(maxsize=10, default_ttl=60)
        await cache.set("k", 1)
        await cache.get("k")
        await cache.get("k")
        assert cache.stats["hits"] == 2


@pytest.mark.unit
class TestMultiLevelCache:
    async def test_l1_hit_no_l2(self) -> None:
        l1 = MemoryCache(maxsize=10, default_ttl=60)
        l2 = MemoryCache(maxsize=10, default_ttl=60)
        cache = MultiLevelCache(memory_cache=l1, redis_cache=l2)
        await cache.set("k", "v")
        assert await cache.get("k") == "v"

    async def test_l1_miss_l2_hit(self) -> None:
        l1 = MemoryCache(maxsize=10, default_ttl=60)
        l2 = MemoryCache(maxsize=10, default_ttl=60)
        await l2.set("k", "v")
        cache = MultiLevelCache(memory_cache=l1, redis_cache=l2)
        assert await cache.get("k") == "v"
        assert await l1.get("k") == "v"  # 回填 L1

    async def test_get_or_set_callback(self) -> None:
        l1 = MemoryCache(maxsize=10, default_ttl=60)
        l2 = MemoryCache(maxsize=10, default_ttl=60)
        cache = MultiLevelCache(memory_cache=l1, redis_cache=l2)
        source = AsyncMock(return_value="computed")
        result = await cache.get_or_set("k", callback=source)
        assert result == "computed"
        source.assert_awaited_once()
