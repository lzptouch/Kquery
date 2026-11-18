"""令牌桶限流。

- 粒度：按租户独立成桶（避免单租户拖垮全局，也防止共享账号绕过）；
- 令牌可为小数，按"距上次回填的时间 × 速率"补足，且不得超过桶容量；
- 拒绝时显式给出 Retry-After 秒数，前端展示倒计时后自动重试一次。

注意：多副本部署时桶状态必须跨副本共享（如 Redis），否则每个副本各算一份，
实际放行量是配置值的 N 倍——这是限流最常见的失效原因。本实现为进程内
单实例版本，接口保持可替换。
"""
import threading
import time
from dataclasses import dataclass
from app.core.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class Bucket:
    tokens: float  # 剩余令牌，可为小数
    last_refill_at: float  # 上次回填时刻
    capacity: float  # 桶容量上限


class TokenBucketLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, tenant_id: str, rate_per_min: float, capacity: float) -> tuple[bool, int]:
        """尝试消费一枚令牌。返回 (是否放行, 未放行时的 Retry-After 秒数)。"""
        now = time.time()
        with self._lock:
            bucket = self._buckets.get(tenant_id)
            if bucket is None or bucket.capacity != capacity:
                # 新租户或容量配置变更：重置为满桶
                bucket = Bucket(tokens=capacity, last_refill_at=now, capacity=capacity)
                self._buckets[tenant_id] = bucket
            # 按时间差回填，不得超过容量（否则空闲期会积累出无限额度）
            elapsed = now - bucket.last_refill_at
            bucket.tokens = min(capacity, bucket.tokens + elapsed * (rate_per_min / 60.0))
            bucket.last_refill_at = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True, 0
            # 计算下一枚令牌的等待时长
            wait = (1.0 - bucket.tokens) / (rate_per_min / 60.0) if rate_per_min > 0 else 60.0
            return False, max(1, int(wait) + 1)

    def bucket_state(self, tenant_id: str) -> Bucket | None:
        return self._buckets.get(tenant_id)


def build_limiter(state=None) -> TokenBucketLimiter:
    """令牌桶工厂。

    配了共享存储就用 Redis 版（多副本共享同一个桶），
    否则用进程内实现（单副本）。
    """
    if state is not None and getattr(state, "redis_enabled", False):
        return RedisTokenBucketLimiter(state)
    return TokenBucketLimiter()


# ---- Redis 版：Lua 保证"回填 + 扣减"原子 ----

# KEYS[1]=桶键；ARGV: rate(每秒补多少) capacity now ttl
# 返回 {allowed(0/1), remaining}
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local tokens = tonumber(redis.call('HGET', key, 'tokens'))
local ts = tonumber(redis.call('HGET', key, 'ts'))
if tokens == nil then tokens = capacity end
if ts == nil then ts = now end
if now > ts then
  tokens = math.min(capacity, tokens + (now - ts) * rate)
end
local allowed = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return {allowed, tostring(tokens)}
"""


class RedisTokenBucketLimiter:
    """共享存储版令牌桶（多副本共享同一份桶状态）。

    **降级纪律**：Redis 不可用 → 回落到进程内桶（单副本语义），
    绝不把"Redis 挂了"变成"请求全被限流"或"请求全被放行"。
    """

    def __init__(self, state, fallback: TokenBucketLimiter | None = None) -> None:
        self._state = state
        self._fallback = fallback or TokenBucketLimiter()
        self._degraded = 0

    def allow(self, tenant_id: str, rate_per_min: float, capacity: float) -> tuple[bool, int]:
        now = time.time()
        try:
            result = self._state.eval(
                _TOKEN_BUCKET_LUA,
                [f"ratelimit:bucket:{tenant_id}"],
                [rate_per_min / 60.0, capacity, now, 300],
            )
            allowed = int(result[0]) == 1
            remaining = float(result[1]) if len(result) > 1 else 0.0
        except Exception:  # noqa: BLE001 - Fail-Open：降级为进程内
            logger.warning("共享状态不可用：Fail-Open 降级为进程内限流（额度按单副本计）")
            self._degraded += 1
            return self._fallback.allow(tenant_id, rate_per_min, capacity)
        if allowed:
            return True, 0
        rate_per_s = rate_per_min / 60.0
        wait = (1.0 - remaining) / rate_per_s if rate_per_s > 0 else 60.0
        return False, max(1, int(wait) + 1)

    def bucket_state(self, tenant_id: str) -> Bucket | None:
        return self._fallback.bucket_state(tenant_id)

    def stats(self) -> dict:
        return {"backend": "redis", "degraded_operations": self._degraded}


def resolve_bucket_limits(active_config, settings) -> tuple[float, float]:
    """令牌桶参数的取值优先级：运行时热配置 > 全局配置。

    此前两个配置项声明后从未被读取，实际生效的永远是 RetrievalConfig 里的硬编码默认
    ——改环境变量没有任何效果。
    """
    rate = getattr(active_config, "ratelimit_per_min", None) or settings.ratelimit_refill_per_min
    capacity = getattr(active_config, "ratelimit_capacity", None) or settings.ratelimit_capacity
    return float(rate), float(capacity)
