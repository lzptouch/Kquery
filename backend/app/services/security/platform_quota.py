"""平台侧配额监控（阶段 9 §6.2）。

**平台侧配额是应用侧配额的上游约束**：应用侧滑动窗口算得再精细，也无法阻止
整个账号维度被千问平台限流。因此这里记录"全账号对平台的调用量与失败构成"，
用于提前预警"整个系统即将不可用"——这是本地部署模型方案里不存在的风险。

与应用侧配额的区别：

| 类型 | 约束对象 | 超限表现 | 治理手段 |
| --- | --- | --- | --- |
| 平台侧（本模块） | 整个账号对千问平台的调用总量 | 平台限流 / 配额错误，**影响所有租户** | 账号级额度规划、告警、多 Key 分流 |
| 应用侧（SlidingWindowQuota） | 单个租户 / 用户 | 应用返回 429，**只影响该租户** | 滑动窗口与维度组合 |

实现为**模块级单例观察者**：各模型客户端（LLM / Embedding / Rerank）在真实调用
前后上报，网关读取快照。默认未配置预算时为空转（不改变任何既有行为）。
"""
import threading
import time


class PlatformQuotaMonitor:
    """账号级平台调用计数与失败构成（按自然日滚动）。"""

    def __init__(self, per_day: int = 0, warn_ratio: float = 0.8):
        self._lock = threading.Lock()
        self.per_day = max(per_day, 0)  # 0 = 未配置预算（只统计不拦截）
        self.warn_ratio = warn_ratio
        self._day = ""
        self._calls = 0
        self._models: dict[str, int] = {}
        self._errors: dict[str, int] = {}

    # ---------- 上报 ----------

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    def _roll(self) -> None:
        """跨自然日归零（调用方需持锁）。"""
        today = self._today()
        if self._day != today:
            self._day = today
            self._calls = 0
            self._models = {}
            self._errors = {}

    def record_call(self, model: str = "", tokens: int = 0) -> None:
        with self._lock:
            self._roll()
            self._calls += 1
            if model:
                self._models[model] = self._models.get(model, 0) + 1

    def record_platform_error(self, kind: str) -> None:
        with self._lock:
            self._roll()
            self._errors[kind] = self._errors.get(kind, 0) + 1

    # ---------- 读取 ----------

    def enabled(self) -> bool:
        return self.per_day > 0

    def exhausted(self) -> bool:
        """账号级预算是否已用尽（上游约束触发点）。"""
        if not self.enabled():
            return False
        with self._lock:
            self._roll()
            return self._calls >= self.per_day

    @staticmethod
    def seconds_until_reset() -> int:
        """距自然日（UTC）归零的秒数，用作 6201 的 Retry-After。"""
        now = time.gmtime()
        return int((24 - now.tm_hour - 1) * 3600 + (59 - now.tm_min) * 60 + (60 - now.tm_sec))

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            calls = self._calls
            models = dict(self._models)
            errors = dict(self._errors)
            day = self._day
        if not self.enabled():
            status = "disabled"
            remaining = None
            ratio = 0.0
        else:
            ratio = calls / self.per_day
            remaining = max(self.per_day - calls, 0)
            if calls >= self.per_day:
                status = "exhausted"
            elif ratio >= self.warn_ratio:
                status = "warning"
            else:
                status = "ok"
        return {
            "budget_per_day": self.per_day,
            "used_today": calls,
            "remaining": remaining,
            "ratio": round(ratio, 4),
            "status": status,
            "day": day,
            "models": models,
            "platform_errors": {
                "rate_limited": errors.get("rate_limited", 0),
                "quota": errors.get("quota", 0),
                "timeout": errors.get("timeout", 0),
                "other": errors.get("other", 0),
            },
        }


_MONITOR: PlatformQuotaMonitor | None = None


def install_monitor(monitor: PlatformQuotaMonitor) -> None:
    """装配期安装全局监控器（各模型客户端通过 get_monitor 上报）。"""
    global _MONITOR
    _MONITOR = monitor


def get_monitor() -> PlatformQuotaMonitor:
    """获取全局监控器；未安装时惰性创建空转实例（测试友好）。"""
    global _MONITOR
    if _MONITOR is None:
        _MONITOR = PlatformQuotaMonitor()
    return _MONITOR
