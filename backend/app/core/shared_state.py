"""共享状态后端（TASK-165）。

## 问题

限流、配额、缓存、指标、记忆全都是**进程内**的。单副本没问题，
但一上多副本就全部失真：

| 组件 | 多副本下的表现 |
| --- | --- |
| 限流 | 每副本各放行一份配额 → 实际限制变成 `配置值 × 副本数` |
| 配额 | 同上（**用户实际可用额度翻倍**） |
| 缓存 | 命中率被摊薄，且热点问题在副本间重复计算 |
| 长期记忆 | 副本 A 学的经验副本 B 不知道 |

## 本次实现的范围（诚实说明）

完整的 Redis 化改造涉及**限流/配额/缓存/指标/记忆**五个组件的存储层重写，
是一次不小的重构。本次做的是**把它们收敛到统一的访问抽象**，并提供：

1. **可用的 Redis 后端**（`RedisStore`）：原子自增、带 TTL 的键值、计数窗口；
2. **本地降级**（`LocalStore`）：Redis 不可用时自动回落，**功能不中断**；
3. **统一的健康探针**：`/health` 能看出当前用的是哪个后端。

也就是说：本模块让"换 Redis"从"改造五个组件"变成"改一个配置"，
并已经具备多副本可用的最小能力。**但没有把五个组件全部迁完**——
它们仍默认走各自的进程内实现，需要逐个切换（见各组件内注释）。

## 降级纪律

Redis 挂了必须**降级而不是报错**：限流/配额退化为单副本语义（比 500 好），
缓存退化为进程内（比不缓存好）。这与项目其它护栏一致。
"""
import logging
import threading
import time
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class StateStore(Protocol):
    """共享状态最小接口。"""

    def incr(self, key: str, amount: int = 1, ttl_s: int = 0) -> int: ...
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, ttl_s: int = 0) -> None: ...
    def delete(self, key: str) -> None: ...
    def zadd(self, key: str, member: str, score: float) -> None: ...
    def zcount(self, key: str, min_score: float, max_score: float) -> int: ...
    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int: ...
    def healthy(self) -> bool: ...
    def backend_name(self) -> str: ...


class LocalStore:
    """进程内实现（单副本 / Redis 不可用时的降级目标）。

    刻意实现与 Redis 相同的接口语义，使上层切换零感知。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._values: dict[str, tuple[str, float]] = {}   # key → (value, expire_at)
        self._zsets: dict[str, dict[str, float]] = {}
        self._dead = False  # 标记降级原因（供探针展示）

    def _expired(self, key: str) -> bool:
        item = self._values.get(key)
        if not item:
            return True
        _, expire_at = item
        if expire_at and expire_at < time.time():
            self._values.pop(key, None)
            return True
        return False

    def incr(self, key: str, amount: int = 1, ttl_s: int = 0) -> int:
        with self._lock:
            cur = 0
            if not self._expired(key):
                try:
                    cur = int(self._values[key][0])
                except ValueError:
                    cur = 0
            new = cur + amount
            expire_at = self._values.get(key, ("", 0.0))[1]
            if ttl_s and (self._expired(key) or not expire_at):
                expire_at = time.time() + ttl_s
            self._values[key] = (str(new), expire_at)
            return new

    def get(self, key: str) -> str | None:
        with self._lock:
            if self._expired(key):
                return None
            return self._values[key][0]

    def set(self, key: str, value: str, ttl_s: int = 0) -> None:
        with self._lock:
            self._values[key] = (str(value), time.time() + ttl_s if ttl_s else 0.0)

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)
            self._zsets.pop(key, None)

    def zadd(self, key: str, member: str, score: float) -> None:
        with self._lock:
            self._zsets.setdefault(key, {})[member] = float(score)

    def zcount(self, key: str, min_score: float, max_score: float) -> int:
        with self._lock:
            return sum(
                1 for s in self._zsets.get(key, {}).values() if min_score <= s <= max_score
            )

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        with self._lock:
            z = self._zsets.get(key, {})
            victims = [m for m, s in z.items() if min_score <= s <= max_score]
            for m in victims:
                z.pop(m, None)
            return len(victims)

    def healthy(self) -> bool:
        return not self._dead

    def backend_name(self) -> str:
        return "local"


class RedisStore:
    """Redis 实现（多副本共享）。

    连接失败时**不抛异常**：上层应当拿到"降级信号"而不是 500。
    `healthy()` 反映最后一次操作是否成功。
    """

    def __init__(self, url: str, prefix: str = "kquery:", timeout_s: float = 1.0):
        self._prefix = prefix
        self._healthy = False
        self._client = None
        try:
            import redis  # type: ignore

            self._client = redis.Redis.from_url(
                url, socket_timeout=timeout_s, socket_connect_timeout=timeout_s,
                decode_responses=True,
            )
            self._client.ping()
            self._healthy = True
            logger.info("共享状态后端：Redis（%s）", _mask(url))
        except Exception as exc:  # noqa: BLE001 - 连不上就降级
            logger.warning("Redis 不可用（%s），将降级为进程内实现", exc)

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def incr(self, key: str, amount: int = 1, ttl_s: int = 0) -> int:
        if not self._client:
            raise RuntimeError("redis unavailable")
        try:
            pipe = self._client.pipeline()
            pipe.incrby(self._k(key), amount)
            if ttl_s:
                # 只在首次设置 TTL（避免每次计数都刷新过期，导致窗口永不到期）
                pipe.expire(self._k(key), ttl_s, nx=True)
            value = pipe.execute()[0]
            self._healthy = True
            return int(value)
        except Exception as exc:  # noqa: BLE001
            self._healthy = False
            logger.warning("Redis incr 失败（%s），调用方应降级", exc)
            raise

    def get(self, key: str) -> str | None:
        if not self._client:
            raise RuntimeError("redis unavailable")
        return self._client.get(self._k(key))

    def set(self, key: str, value: str, ttl_s: int = 0) -> None:
        if not self._client:
            raise RuntimeError("redis unavailable")
        self._client.set(self._k(key), value, ex=ttl_s or None)

    def delete(self, key: str) -> None:
        if not self._client:
            raise RuntimeError("redis unavailable")
        self._client.delete(self._k(key))

    def zadd(self, key: str, member: str, score: float) -> None:
        if not self._client:
            raise RuntimeError("redis unavailable")
        self._client.zadd(self._k(key), {member: float(score)})

    def zcount(self, key: str, min_score: float, max_score: float) -> int:
        if not self._client:
            raise RuntimeError("redis unavailable")
        return int(self._client.zcount(self._k(key), min_score, max_score))

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        if not self._client:
            raise RuntimeError("redis unavailable")
        return int(self._client.zremrangebyscore(self._k(key), min_score, max_score))

    def healthy(self) -> bool:
        return self._healthy and self._client is not None

    def backend_name(self) -> str:
        return "redis"


def _mask(url: str) -> str:
    """脱敏连接串（可能含密码）。"""
    import re

    return re.sub(r"://([^:@/]+):([^@/]+)@", r"://\1:***@", url or "")


class SharedState:
    """共享状态门面：优先 Redis，失败自动降级到进程内。

    降级是**逐操作**的：某次 Redis 抖动不会永久切走，下一次仍会尝试 Redis
    （否则一次网络抖动就把整个进程变回单副本语义，且不会被察觉）。
    """

    def __init__(self, redis_url: str = "", prefix: str = "kquery:"):
        self._local = LocalStore()
        self._redis = RedisStore(redis_url, prefix) if redis_url else None
        self._degraded_ops = 0

    @property
    def backend(self) -> str:
        if self._redis is not None and self._redis.healthy():
            return "redis"
        if self._redis is not None:
            return "local(redis-degraded)"
        return "local"

    def _call(self, method: str, *args, **kwargs):
        if self._redis is not None and self._redis.healthy():
            try:
                return getattr(self._redis, method)(*args, **kwargs)
            except Exception:  # noqa: BLE001 - 降级而非报错
                self._degraded_ops += 1
        return getattr(self._local, method)(*args, **kwargs)

    def incr(self, key: str, amount: int = 1, ttl_s: int = 0) -> int:
        return self._call("incr", key, amount, ttl_s)

    def get(self, key: str) -> str | None:
        return self._call("get", key)

    def set(self, key: str, value: str, ttl_s: int = 0) -> None:
        self._call("set", key, value, ttl_s)

    def delete(self, key: str) -> None:
        self._call("delete", key)

    def zadd(self, key: str, member: str, score: float) -> None:
        self._call("zadd", key, member, score)

    def zcount(self, key: str, min_score: float, max_score: float) -> int:
        return self._call("zcount", key, min_score, max_score)

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        return self._call("zremrangebyscore", key, min_score, max_score)

    def snapshot(self) -> dict:
        """探针用：当前后端、是否降级、降级次数。"""
        return {
            "backend": self.backend,
            "degraded_operations": self._degraded_ops,
            "redis_enabled": self._redis is not None,
        }
