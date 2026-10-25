"""滑动窗口配额。

- 按租户（不按用户——共享账号会绕过用户级限流）；
- 三个维度：分钟滑动窗口（**真滑动**：秒级时间戳队列，任意 60s 内不超上限，
  无固定窗口在分钟边界的双倍突刺）、每日调用次数、每日 token 消耗；
- 计数与判定原子（锁内完成"查+加"，并发不放行超额请求）；
- 超限 429 + Retry-After。

窗口状态在进程内。多副本部署必须换成共享存储（Redis ZSET），否则实际放行量
是配置值的 N 倍——这是限流最常见的失效原因。接口保持不变以便替换。
"""
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field


@dataclass
class QuotaPolicy:
    per_minute: int = 60  # 分钟窗口上限
    per_day: int = 10000  # 每日调用上限
    token_per_day: int = 0  # 每日 token 消耗上限（0 = 不限制该维度）


@dataclass
class _TenantWindow:
    # 真滑动窗口：保存最近 60s 内每次调用的时间戳，出队即释放
    hits: deque = field(default_factory=deque)
    day_bucket: tuple = ("", 0)  # (日期, 调用次数)
    token_bucket: tuple = ("", 0)  # (日期, token 累计)


class SlidingWindowQuota:
    WINDOW_S = 60.0

    def __init__(self, state=None):
        self._lock = threading.Lock()
        self._windows = defaultdict(_TenantWindow)
        self._policies = {}
        # 共享存储（Redis）：多副本下配额必须全局生效，
        # 否则每个副本各放一份，实际额度 = 配置值 × 副本数。
        self._state = state
        self._degraded = 0

    def set_policy(self, tenant_id, per_minute, per_day, token_per_day=0):
        with self._lock:
            self._policies[tenant_id] = QuotaPolicy(per_minute, per_day, token_per_day)

    def _policy(self, tenant_id):
        return self._policies.get(tenant_id, QuotaPolicy())

    @classmethod
    def _trim(cls, window, now):
        cutoff = now - cls.WINDOW_S
        while window.hits and window.hits[0] <= cutoff:
            window.hits.popleft()

    def check_and_consume(self, tenant_id, tokens=0):
        """原子判定并计数。返回 (放行, retry_after_s, 维度)。

        tokens 为本次请求预计消耗的 token 数（0 = 不计入 token 维度）；
        请求结束后应再用 record_tokens 按实际用量补记，预估值必然有偏差。
        """
        if self._state is not None and getattr(self._state, "redis_enabled", False):
            try:
                return self._check_shared(tenant_id, tokens)
            except Exception:  # noqa: BLE001 - Fail-Open：降级为进程内
                self._degraded += 1
        return self._check_local(tenant_id, tokens)

    # ---- 共享存储实现（Redis）----

    def _check_shared(self, tenant_id, tokens):
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        day_start = time.mktime(time.strptime(today, "%Y-%m-%d"))
        policy = self._policy(tenant_id)

        # ① 分钟滑动窗口（ZSET：member=请求标识，score=时间戳）
        win_key = f"quota:win:{tenant_id}"
        self._state.zremrangebyscore(win_key, 0, now - self.WINDOW_S)
        sliding = self._state.zcount(win_key, now - self.WINDOW_S, now)
        if sliding >= policy.per_minute:
            return False, max(1, int(self.WINDOW_S) + 1), "minute"

        # ② 每日调用次数（另一个 ZSET，score=当天零点）
        day_key = f"quota:day:{tenant_id}"
        self._state.zremrangebyscore(day_key, 0, day_start - 1)
        day_count = self._state.zcount(day_key, day_start, day_start + 86400)
        if day_count >= policy.per_day:
            return False, self._seconds_until_midnight(), "daily"

        # ③ 每日 token 消耗（计数器）
        tok_key = f"quota:tok:{tenant_id}:{today}"
        if policy.token_per_day:
            used = int(self._state.get(tok_key) or 0)
            if used + max(tokens, 0) > policy.token_per_day:
                return False, self._seconds_until_midnight(), "token_daily"

        # 放行：登记本次调用
        member = f"{now}:{id(now)}:{now}"
        self._state.zadd(win_key, member, now)
        self._state.zadd(day_key, member, day_start)
        if policy.token_per_day and tokens > 0:
            self._state.incr(tok_key, int(tokens), 86400)
        return True, 0, ""

    # ---- 进程内实现（单副本 / 降级目标）----

    def _check_local(self, tenant_id, tokens=0):
        now = time.time()
        # 按本地时区分桶：UTC 会让「每日配额」在用户时区的中途重置，与预期不符
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        with self._lock:
            policy = self._policy(tenant_id)
            window = self._windows[tenant_id]
            self._trim(window, now)

            day_date, day_count = window.day_bucket
            if day_date != today:
                day_count = 0
            tok_date, tok_count = window.token_bucket
            if tok_date != today:
                tok_count = 0

            if day_count >= policy.per_day:
                return False, self._seconds_until_midnight(), "daily"
            if policy.token_per_day and tok_count + max(tokens, 0) > policy.token_per_day:
                return False, self._seconds_until_midnight(), "token_daily"
            if len(window.hits) >= policy.per_minute:
                # 最早那条出窗还需等待的时长
                wait = int(window.hits[0] + self.WINDOW_S - now) + 1
                return False, max(1, wait), "minute"

            window.hits.append(now)
            window.day_bucket = (today, day_count + 1)
            window.token_bucket = (today, tok_count + max(tokens, 0))
            return True, 0, ""

    def record_tokens(self, tenant_id, tokens):
        """请求结束后按实际 token 用量补记。"""
        if tokens <= 0:
            return
        today = time.strftime("%Y-%m-%d", time.localtime(time.time()))
        if self._state is not None and getattr(self._state, "redis_enabled", False):
            try:
                self._state.incr(f"quota:tok:{tenant_id}:{today}", int(tokens), 86400)
                return
            except Exception:  # noqa: BLE001 - Fail-Open：降级为进程内
                self._degraded += 1
        with self._lock:
            window = self._windows[tenant_id]
            tok_date, tok_count = window.token_bucket
            window.token_bucket = (today if tok_date != today else tok_date, tok_count + tokens)

    @staticmethod
    def _seconds_until_midnight():
        now = time.localtime()
        return int((24 - now.tm_hour - 1) * 3600 + (59 - now.tm_min) * 60 + (60 - now.tm_sec))

    def overview(self, tenants):
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        with self._lock:
            rows = []
            for tenant in tenants:
                policy = self._policy(tenant)
                window = self._windows.get(tenant, _TenantWindow())
                self._trim(window, now)
                sliding = len(window.hits)
                day_count = window.day_bucket[1] if window.day_bucket[0] == today else 0
                tok_count = window.token_bucket[1] if window.token_bucket[0] == today else 0
                ratio = max(
                    sliding / max(policy.per_minute, 1),
                    day_count / max(policy.per_day, 1),
                    (tok_count / policy.token_per_day) if policy.token_per_day else 0.0,
                )
                rows.append({
                    "tenant_id": tenant,
                    "limit_per_minute": policy.per_minute,
                    "limit_per_day": policy.per_day,
                    "limit_token_per_day": policy.token_per_day,
                    "current_window": sliding,
                    "today_count": day_count,
                    "today_tokens": tok_count,
                    "status": "exceeded" if ratio >= 1 else "warning" if ratio > 0.8 else "ok",
                })
            return rows
