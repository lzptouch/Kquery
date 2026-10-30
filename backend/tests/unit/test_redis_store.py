"""F6 共享存储单测：降级包装、跨实例共享、Redis 挂掉仍可用。"""
import time

import pytest

from app.core.shared_state import LocalStore, RedisStore, SharedState, StoreUnavailable
from app.services.cache import RedisSemanticCache, build_cache
from app.services.ratelimit import RedisTokenBucketLimiter, TokenBucketLimiter, build_limiter


class _FakeRedis:
    """最小 Redis 替身：够用即可（get/set/delete/exists/incr/zsets）。"""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.down = False

    # ---- 字符串 ----
    def get(self, key):
        self._check()
        return self.strings.get(key)

    def set(self, key, value, ex=None):
        self._check()
        self.strings[key] = value

    def delete(self, key):
        self._check()
        self.strings.pop(key, None)
        self.zsets.pop(key, None)

    def exists(self, key):
        self._check()
        return 1 if (key in self.strings or key in self.zsets) else 0

    def ping(self):
        self._check()
        return True

    def incrby(self, key, amount):
        self._check()
        self.strings[key] = str(int(self.strings.get(key, "0")) + amount)
        return int(self.strings[key])

    def expire(self, key, ttl, nx=False):
        self._check()
        return True

    def pipeline(self):
        self._check()
        return _FakePipeline(self)

    # ---- ZSET ----
    def zadd(self, key, mapping):
        self._check()
        self.zsets.setdefault(key, {}).update({k: float(v) for k, v in mapping.items()})

    def zcount(self, key, min_score, max_score):
        self._check()
        z = self.zsets.get(key, {})
        return sum(1 for s in z.values() if min_score <= s <= max_score)

    def zremrangebyscore(self, key, min_score, max_score):
        self._check()
        z = self.zsets.get(key, {})
        victims = [m for m, s in z.items() if min_score <= s <= max_score]
        for m in victims:
            z.pop(m, None)
        return len(victims)

    def eval(self, script, numkeys, *keys_and_args):
        """按 redis-py 的真实签名：eval(script, numkeys, *keys_and_args)。

        刻意不写成 eval(script, keys, args)——那样会放过
        "把 keys/args 当两个列表传"的错误（redis-py 会把第 2 个位置参数
        当 numkeys，直接报 Invalid input of type: 'list'），
        而线上表现是"每次限流都悄悄降级到进程内"，很难发现。
        """
        self._check()
        if not isinstance(numkeys, int):
            raise TypeError(
                f"numkeys 必须是 int，收到 {type(numkeys).__name__}"
                "（redis-py 的签名是 eval(script, numkeys, *keys_and_args)）"
            )
        keys = list(keys_and_args[:numkeys])
        args = list(keys_and_args[numkeys:])
        # 令牌桶脚本的最小实现：够测试用
        key = keys[0]
        rate, capacity, now, _ttl = (float(a) for a in args)
        h = self.hashes.setdefault(key, {})
        tokens = float(h.get("tokens", capacity))
        ts = float(h.get("ts", now))
        if now > ts:
            tokens = min(capacity, tokens + (now - ts) * rate)
        allowed = 0
        if tokens >= 1:
            tokens -= 1
            allowed = 1
        h["tokens"] = str(tokens)
        h["ts"] = str(now)
        return [allowed, str(tokens)]

    def _check(self):
        if self.down:
            raise ConnectionError("redis down")


class _FakePipeline:
    def __init__(self, client):
        self._client = client
        self._ops: list[tuple[str, tuple]] = []

    def incrby(self, key, amount):
        self._ops.append(("incrby", (key, amount)))
        return self

    def expire(self, key, ttl, nx=False):
        self._ops.append(("expire", (key, ttl)))
        return self

    def execute(self):
        results = []
        for name, args in self._ops:
            if name == "incrby":
                results.append(self._client.incrby(*args))
            else:
                results.append(self._client.expire(*args))
        return results


def _redis_store(client: _FakeRedis) -> RedisStore:
    store = RedisStore.__new__(RedisStore)
    store._prefix = "kquery:"
    store._healthy = True
    store._client = client
    store._url = "redis://fake:6379/0"
    store._timeout_s = 1.0
    store._last_probe = 0.0
    return store


class TestRedisStoreDegradation:
    def test_fallback_when_client_none(self):
        """无客户端 → 抛 StoreUnavailable（不是 RuntimeError）。"""
        store = RedisStore.__new__(RedisStore)
        store._prefix = "kquery:"
        store._healthy = False
        store._client = None
        for call in (
            lambda: store.get("k"),
            lambda: store.set("k", "v"),
            lambda: store.delete("k"),
            lambda: store.exists("k"),
            lambda: store.incr("k"),
        ):
            with pytest.raises(StoreUnavailable):
                call()

    def test_store_unavailable_is_runtime_error(self):
        """StoreUnavailable 是 RuntimeError 的子类：老的 except RuntimeError 仍能兜住。"""
        assert issubclass(StoreUnavailable, RuntimeError)

    def test_redis_down_raises_store_unavailable(self):
        client = _FakeRedis()
        store = _redis_store(client)
        client.down = True
        with pytest.raises(StoreUnavailable):
            store.get("k")

    def test_exists_supported(self):
        client = _FakeRedis()
        store = _redis_store(client)
        assert store.exists("nope") is False
        store.set("k", "v")
        assert store.exists("k") is True


class TestSharedState:
    def test_falls_back_to_local_when_redis_down(self):
        client = _FakeRedis()
        client.down = True
        store = RedisStore.__new__(RedisStore)
        store._prefix = "kquery:"
        store._healthy = False
        store._client = client
        store._url = "redis://fake:6379/0"
        store._timeout_s = 1.0
        store._last_probe = 0.0
        state = SharedState.__new__(SharedState)
        state._local = LocalStore()
        state._redis = store
        state._degraded_ops = 0

        # Fail-Open：Redis 挂了依然能读写（落到进程内）
        state.set("k", "v")
        assert state.get("k") == "v"
        assert state.exists("k") is True
        assert state.backend.startswith("local")

    def test_local_store_exists(self):
        local = LocalStore()
        assert local.exists("k") is False
        local.set("k", "v")
        assert local.exists("k") is True
        local.delete("k")
        assert local.exists("k") is False


class TestRedisRecovery:
    def test_recovers_after_transient_failure(self):
        """一次失败不能永久毒化共享状态。

        真实事故：Redis Lua 调用参数写错 → 每次都失败 → `_healthy=False`
        → 之后**所有** Redis 操作（缓存/限流/配额/快问元数据）静默走进程内，
        多副本共享直接失效，而日志只有一行警告。
        """
        client = _FakeRedis()
        store = _redis_store(client)
        state = SharedState.__new__(SharedState)
        state._local = LocalStore()
        state._redis = store
        state._degraded_ops = 0

        client.down = True
        state.set("k", "v")          # 失败 → 降级
        assert state._degraded_ops >= 1

        client.down = False
        store._last_probe = 0.0      # 绕过重连冷却
        state.set("k2", "v2")        # 应恢复并写进 Redis
        assert client.strings, "恢复后应写进 Redis，而不是一直留在进程内"
        assert "kquery:k2" in client.strings

    def test_reconnect_respects_cooldown(self):
        client = _FakeRedis()
        store = _redis_store(client)
        store._healthy = False
        client.down = True
        assert store._reconnect() is False
        # 冷却期内不再重连
        store._last_probe = time.monotonic()
        assert store._reconnect() is False


class TestRedisSemanticCache:
    def _state(self, client):
        state = SharedState.__new__(SharedState)
        state._local = LocalStore()
        state._redis = _redis_store(client)
        state._degraded_ops = 0
        return state

    def test_cache_shared_across_instances(self):
        """两个实例（模拟两个副本）共享同一后端 → 第二个能命中第一个的写入。"""
        client = _FakeRedis()
        state_a = self._state(client)
        state_b = self._state(client)

        cache_a = RedisSemanticCache(state_a)
        cache_b = RedisSemanticCache(state_b)
        cache_a.set("k1", {"answer": "答案"}, 60, question="问题", tenant_id="t1")

        entry = cache_b.get("k1")
        assert entry is not None, "跨实例必须能读到共享存储里的条目"
        assert entry.payload["answer"] == "答案"

    def test_scope_isolation(self):
        client = _FakeRedis()
        cache = RedisSemanticCache(self._state(client))
        cache.set("k1", {"answer": "A"}, 60, question="q", tenant_id="t1", scope="doc_a")
        cache.set("k2", {"answer": "B"}, 60, question="q", tenant_id="t1", scope="doc_b")
        assert cache.get("k1").payload["answer"] == "A"
        assert cache.get("k2").payload["answer"] == "B"

    def test_store_failure_does_not_raise(self):
        """Redis 挂掉 → 共享状态门面自动降级为进程内，读写照常（Fail-Open）。"""
        client = _FakeRedis()
        cache = RedisSemanticCache(self._state(client))
        client.down = True
        cache.set("k", {"answer": "x"}, 60)  # 不抛异常
        entry = cache.get("k")
        assert entry is not None, "降级后缓存仍可用（只是不再跨副本共享）"
        assert entry.payload["answer"] == "x"

    def test_build_cache_selects_backend(self):
        client = _FakeRedis()
        state = self._state(client)
        assert isinstance(build_cache(provider="redis", state=state), RedisSemanticCache)
        assert isinstance(build_cache(provider="memory"), type(build_cache(provider="memory")))
        assert not isinstance(build_cache(provider="memory", state=state), RedisSemanticCache)


class TestRedisLimiter:
    def test_limiter_uses_redis_not_fallback(self):
        """Redis 可用时限流必须走 Redis（而不是静默降级成进程内桶）。"""
        client = _FakeRedis()
        state = SharedState.__new__(SharedState)
        state._local = LocalStore()
        state._redis = _redis_store(client)
        state._degraded_ops = 0

        limiter = RedisTokenBucketLimiter(state)
        allowed, _ = limiter.allow("t1", 60, 60)
        assert allowed is True
        assert limiter.stats()["degraded_operations"] == 0, "限流不应降级"
        assert client.hashes, "令牌桶状态应写进 Redis"

    def test_limiter_falls_back_when_redis_down(self):
        client = _FakeRedis()
        state = SharedState.__new__(SharedState)
        state._local = LocalStore()
        state._redis = _redis_store(client)
        state._degraded_ops = 0
        client.down = True

        limiter = RedisTokenBucketLimiter(state)
        allowed, _ = limiter.allow("t1", 60, 60)
        assert allowed is True, "Redis 挂掉不得变成请求全被拒"
        assert limiter.stats()["degraded_operations"] >= 1

    def test_build_limiter_picks_redis(self):
        client = _FakeRedis()
        state = SharedState.__new__(SharedState)
        state._local = LocalStore()
        state._redis = _redis_store(client)
        state._degraded_ops = 0
        assert isinstance(build_limiter(state=state), RedisTokenBucketLimiter)
        assert isinstance(build_limiter(), TokenBucketLimiter)


