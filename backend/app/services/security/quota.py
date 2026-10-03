"""滑动窗口配额（阶段 9 §6）。

- 按租户（不按用户——共享账号会绕过用户级限流）；
- 两个维度：分钟滑动窗口（任意 60s 不超上限，无固定窗口双倍突刺）+ 每日调用次数；
- 计数与判定原子（锁内完成"查+加"，并发不放行超额请求）；
- 按时间分桶整桶淘汰（不每次全量清理）；
- 超限 429 + Retry-After。
"""
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class QuotaPolicy:
    per_minute: int = 60  # 分钟窗口上限
    per_day: int = 10000  # 每日调用上限


@dataclass
class _TenantWindow:
    minute_buckets: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    day_bucket: tuple[str, int] = ("", 0)  # (日期, 计数)


class SlidingWindowQuota:
    def __init__(self):
        self._lock = threading.Lock()
        self._windows: dict[str, _TenantWindow] = defaultdict(_TenantWindow)
        self._policies: dict[str, QuotaPolicy] = {}

    def set_policy(self, tenant_id: str, per_minute: int, per_day: int) -> None:
        with self._lock:
            self._policies[tenant_id] = QuotaPolicy(per_minute, per_day)

    def _policy(self, tenant_id: str) -> QuotaPolicy:
        return self._policies.get(tenant_id, QuotaPolicy())

    def check_and_consume(self, tenant_id: str) -> tuple[bool, int, str]:
        """原子判定。返回 (放行, retry_after_s, 维度)。"""
        now = time.time()
        minute = int(now // 60)
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self._lock:
            policy = self._policy(tenant_id)
            window = self._windows[tenant_id]
            # 整桶淘汰：只保留当前与上一分钟
            for m in [m for m in window.minute_buckets if m < minute - 1]:
                del window.minute_buckets[m]
            current_minute = window.minute_buckets.get(minute, 0)
            # 滑动窗口：当前分钟 + 上一分钟的加权（简化：当前+上分钟计数）
            sliding = current_minute + window.minute_buckets.get(minute - 1, 0)
            day_date, day_count = window.day_bucket
            if day_date != today:
                day_count = 0
            if day_count >= policy.per_day:
                return False, self._seconds_until_midnight(), "daily"
            if sliding >= policy.per_minute:
                return False, 60 - int(now % 60), "minute"
            # 计数与判定同锁内原子完成
            window.minute_buckets[minute] = current_minute + 1
            window.day_bucket = (today, day_count + 1)
            return True, 0, ""

    @staticmethod
    def _seconds_until_midnight() -> int:
        now = time.gmtime()
        return int((24 - now.tm_hour - 1) * 3600 + (59 - now.tm_min) * 60 + (60 - now.tm_sec))

    def overview(self, tenants: list[str]) -> list[dict]:
        now = time.time()
        minute = int(now // 60)
        today = time.strftime("%Y-%m-%d", time.gmtime(now))
        with self._lock:
            rows = []
            for tenant in tenants:
                policy = self._policy(tenant)
                window = self._windows.get(tenant, _TenantWindow())
                sliding = window.minute_buckets.get(minute, 0) + window.minute_buckets.get(minute - 1, 0)
                day_count = window.day_bucket[1] if window.day_bucket[0] == today else 0
                ratio = max(
                    sliding / max(policy.per_minute, 1), day_count / max(policy.per_day, 1)
                )
                rows.append({
                    "tenant_id": tenant,
                    "limit_per_minute": policy.per_minute,
                    "limit_per_day": policy.per_day,
                    "current_window": sliding,
                    "today_count": day_count,
                    "status": "exceeded" if ratio >= 1 else "warning" if ratio > 0.8 else "ok",
                })
            return rows
