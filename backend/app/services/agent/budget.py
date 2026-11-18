"""Agent 三预算治理。

此前 Kquery 只有「轮数」一个维度有上限（iteration / no_progress / step 数），
**没有任何与资源消耗相关的闸门**：

- 无 token 预算 → 一个"反复重规划"的 Agent 可以烧掉任意额度；
- 无成本预算 → 换更贵的模型后无人察觉；
- 无整体 deadline → 上游卡死时请求永久挂起（`runner.py` 的 `events.get()`
  无 timeout，）。

本模块提供三预算的统一记账与判定。设计原则：

1. **预算耗尽不报错，降级作答**（`status="budget_exhausted"`）。预算控制的目的是
   "别把用户和平台的钱烧光"，不是"拒绝服务"——用户至少该拿到基于已有证据的部分答案。
2. **判定用"是否已耗尽"而非"是否会耗尽"**：调用前先问剩余额度够不够本次调用的
   下界，避免执行到一半才发现超限（那时钱已经花了）。
3. **成本按 token 折算**：价格表从配置读取（每千 token 单价，单位：元），
   不引入外部价格服务——过期价格比没有价格更危险，故价格表显式配置且带版本注释。
"""
from dataclasses import dataclass, field
import time


@dataclass
class AgentBudget:
    """单次 Agent 运行的预算账本。

    三个维度任一触发即耗尽：`token` / `cost` / `deadline`。
    """

    token_limit: int = 0          # 0 = 不限
    cost_limit_yuan: float = 0.0  # 0 = 不限
    deadline_ms: int = 0          # 0 = 不限

    # ---- 运行时记账（由 settle_* 维护）----
    tokens_used: int = 0
    cost_used: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    # ---- 判定来源，耗尽时写回供前端解释 ----
    exhausted_reason: str = ""

    # ---- 查询 ----

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def remaining_tokens(self) -> int:
        return max(self.token_limit - self.tokens_used, 0) if self.token_limit else -1

    def exhausted(self) -> str:
        """返回耗尽原因（''/token/cost/deadline），未耗尽返回空串。"""
        if self.exhausted_reason:
            return self.exhausted_reason
        if self.deadline_ms and self.elapsed_ms >= self.deadline_ms:
            return "deadline"
        if self.token_limit and self.tokens_used >= self.token_limit:
            return "token"
        if self.cost_limit_yuan and self.cost_used >= self.cost_limit_yuan:
            return "cost"
        return ""

    def max_output_tokens(self, reserve: int = 0, fallback: int = 0) -> int:
        """本次调用允许的**最大输出 token**（`ST-04`）。

        计算：剩余额度 − 已用（已结算）− 预留（答案生成必须留够）。

        - 返回 **0** = 不限（没有 token 预算，或剩余额度为负 → 调用方不该发起调用）；
        - `fallback`：无预算上下文时的兜底上限（配置项，默认开启）；
        - `reserve`：生成前还要留给后续步骤的 token。

        **为什么必须有上限**：预算在**生成阶段**就可能被打穿——
        `settle_tokens` 记的是**事后**估算，事后才知道超了，钱已经花了。
        """
        remaining = self.remaining_tokens()
        if remaining < 0:
            # 没有 token 预算 → 用配置兜底值
            return max(0, int(fallback))
        allowance = remaining - max(0, int(reserve))
        if allowance <= 0:
            return 0
        if fallback:
            allowance = min(allowance, int(fallback))
        return allowance

    def can_afford(self, tokens: int = 0, cost_yuan: float = 0.0) -> bool:
        """能否承担**下一次**调用的下界开销。

        只看剩余额度是否够这一次——不够就应当拒绝，避免"调用完才发现超限"。
        """
        if self.exhausted():
            return False
        if self.token_limit and self.tokens_used + tokens > self.token_limit:
            return False
        if self.cost_limit_yuan and self.cost_used + cost_yuan > self.cost_limit_yuan:
            return False
        return True

    # ---- 记账 ----

    def settle_tokens(self, tokens: int, cost_yuan: float = 0.0) -> None:
        self.tokens_used += max(0, int(tokens or 0))
        self.cost_used += max(0.0, float(cost_yuan or 0.0))
        reason = self.exhausted()
        if reason:
            self.exhausted_reason = reason

    # ---- 输出 ----

    def snapshot(self) -> dict:
        """进响应体的预算视图（前端据此解释"为什么答得这么快/这么短"）。"""
        return {
            "token_limit": self.token_limit,
            "tokens_used": self.tokens_used,
            "cost_limit_yuan": self.cost_limit_yuan,
            "cost_used_yuan": round(self.cost_used, 6),
            "deadline_ms": self.deadline_ms,
            "elapsed_ms": self.elapsed_ms,
            "exhausted_reason": self.exhausted(),
        }


def estimate_cost_yuan(tokens: int, price_per_1k: dict[str, float], model: str = "") -> float:
    """按模型价格折算成本（元）。

    价格表**显式配置**，不内置也不联网：内置会过期，联网会引入外部依赖与
    故障面。找不到模型时返回 0 并由调用方标记为"成本未知"——宁可显示未知，
    也不给出错误数字（错误数字会被用来做告警与归因）。
    """
    price = price_per_1k.get(model)
    if not price:
        return 0.0
    return (max(0, int(tokens or 0)) / 1000.0) * float(price)


def budget_from_settings(settings, tenant_id: str = "") -> AgentBudget:
    """从配置构造预算账本。

    预算按租户可覆盖（`agent_token_budget_<tenant>` 这类后缀键），便于
    大客户单独放宽；未配置时用全局默认值。
    """
    def _scoped(name: str, default):
        v = getattr(settings, f"{name}_{tenant_id}", None) if tenant_id else None
        return v if v is not None else getattr(settings, name, default)

    return AgentBudget(
        token_limit=int(_scoped("agent_token_budget", 0) or 0),
        cost_limit_yuan=float(_scoped("agent_cost_limit_yuan", 0.0) or 0.0),
        deadline_ms=int(_scoped("agent_deadline_ms", 0) or 0),
    )


def price_table(settings) -> dict[str, float]:
    """解析价格表配置（容错：非法 JSON 视为空表，成本记 0 而非崩溃）。"""
    import json as _json

    raw = getattr(settings, "agent_price_per_1k_json", "") or ""
    try:
        data = _json.loads(raw)
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}
