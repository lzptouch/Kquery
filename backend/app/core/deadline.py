"""请求级 deadline：把"这次请求最多花多久"贯穿到每一个上游调用。

**为什么需要它**：此前只有 Agent 路径有 deadline（`agent_deadline_ms`），
线性问答路径只有"每次 LLM 调用各自 60s 超时"——重试 3 次 + 阻塞退避 ≈183s，
而**请求本身没有任何上限**。没有上限的等待不是慢，是不可控：
上游一慢，网关先超时，用户看到 502，而服务端还在烧 token。

三条纪律：

1. deadline 是**预算**不是**检查点**：每次上游调用都按"剩余额度"裁剪超时，
   而不是走到末尾才发现超了；
2. 重试次数随剩余额度递减：额度不够一次有意义的尝试时直接放弃，
   不做"注定超时"的重试；
3. 退避等待同样受剩余额度约束——阻塞 sleep 是最常见的隐藏超时源。

本模块刻意**不依赖 services 层**（不抛 LLMError）：越界由调用方按自己的
错误语义翻译，避免 core → services 的反向依赖。
"""
from __future__ import annotations

import inspect
import time


def supports_deadline(fn) -> bool:
    """判断一个可调用对象是否接受 ``deadline`` 关键字参数。

    `deadline` 是**可选能力**：第三方适配器与测试替身不一定实现它。
    调用方据此退化为无预算调用，而不是抛 TypeError 让整次请求失败。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # 内建函数 / C 扩展无法 introspect
        return False
    if "deadline" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class Deadline:
    """一次请求的墙钟预算。线程安全（只读自身状态，不共享可变计数器）。"""

    __slots__ = ("_budget_ms", "_start", "_clock")

    def __init__(self, budget_ms: int, clock=time.monotonic) -> None:
        self._budget_ms = max(0, int(budget_ms))
        self._start = clock()
        self._clock = clock

    @property
    def budget_ms(self) -> int:
        return self._budget_ms

    @property
    def elapsed_ms(self) -> int:
        return int((self._clock() - self._start) * 1000)

    @property
    def remaining_ms(self) -> int:
        """剩余额度（毫秒，下限 0）。"""
        return max(0, self._budget_ms - self.elapsed_ms)

    @property
    def expired(self) -> bool:
        return self.remaining_ms <= 0

    def slice_ms(self, ratio: float = 1.0, cap_ms: int | None = None) -> int:
        """给下一段分配额度：剩余额度 × ratio，并受 ``cap_ms`` 上限约束。"""
        value = int(self.remaining_ms * max(0.0, min(1.0, ratio)))
        if cap_ms is not None:
            value = min(value, int(cap_ms))
        return max(0, value)

    def can_wait(self, seconds: float, reserve_s: float = 0.0) -> bool:
        """剩余额度是否够"等 ``seconds`` 之后再试一次（那次至少能跑 ``reserve_s`` 秒）"。"""
        need_ms = int((max(0.0, seconds) + max(0.0, reserve_s)) * 1000)
        return need_ms <= self.remaining_ms

    def wait_s(self, seconds: float) -> bool:
        """按剩余额度等待。返回 False 表示"额度不够等"，调用方应放弃重试。"""
        wait_s = max(0.0, float(seconds))
        if wait_s <= 0:
            return True
        if not self.can_wait(wait_s):
            return False
        time.sleep(wait_s)
        return True
