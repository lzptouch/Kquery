from unittest.mock import AsyncMock, MagicMock
import os

import pytest

from app.core.rate_limiter import RateLimiter


class FakePipeline:
    def __init__(self, existing_requests: int):
        self.existing_requests = existing_requests

    def zrangebyscore(self, *args, **kwargs):
        return []

    def zcard(self, key):
        return self.existing_requests

    async def execute(self):
        return ([], self.existing_requests)


class FakeRedis:
    def __init__(self, existing_requests: int = 0):
        self.existing_requests = existing_requests
        self.added = []

    def pipeline(self):
        return FakePipeline(self.existing_requests)

    async def zadd(self, key, mapping):
        self.added.append((key, mapping))

    async def expire(self, key, seconds):
        pass

    async def close(self):
        pass


@pytest.mark.unit
class TestRateLimiter:
    def setup_method(self):
        # 确保限流逻辑在测试环境中生效（不被 APP_ENV=test 跳过）
        self._orig_app_env = os.environ.get("APP_ENV")
        os.environ["APP_ENV"] = "pytest"

    def teardown_method(self):
        if self._orig_app_env is None:
            os.environ.pop("APP_ENV", None)
        else:
            os.environ["APP_ENV"] = self._orig_app_env

    async def test_allow_within_limit(self) -> None:
        limiter = RateLimiter()
        limiter.redis = FakeRedis(existing_requests=0)
        result = await limiter.check_rate_limit("key", limit_type="ip")
        assert result["allowed"] is True
        assert result["remaining"] >= 0

    async def test_deny_over_limit(self) -> None:
        from app.core.exceptions import RateLimitError

        limiter = RateLimiter()
        limiter.redis = FakeRedis(existing_requests=999999)
        with pytest.raises(RateLimitError):
            await limiter.check_rate_limit("key", limit_type="ip")

    async def test_redis_none_allows(self) -> None:
        limiter = RateLimiter()
        limiter.redis = None
        # 降级：Redis 不可用时通常允许通过
        result = await limiter.check_rate_limit("key", limit_type="ip")
        assert result["allowed"] is True
