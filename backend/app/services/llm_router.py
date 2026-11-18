"""模型路由与降级（`LM-01`）。

## 为什么需要

上游 429 / 超时 / 5xx 时，此前只有两个选择：直接失败，或退到「通用知识兜底」
（**内容降级**，用户看不出区别，只觉得答案变差了）。

模型降级是**另一种**降级：换个模型继续答同样的问题，答案质量基本不变，
而「本次用了降级模型」是**可见的**。

## 三条硬纪律

1. **只对可重试的失败降级**：鉴权（401/403）与参数错（400）换个模型也一样失败——
   降级只会把「配置错了」伪装成「模型不行」。
2. **熔断打开不降级**：熔断说明**整条上游通道**病了，换同平台的另一个模型
   无济于事（同一个 Key、同一个域名）。
3. **全部候选失败 → 抛原异常**（不吞）：路由器不制造「看起来成功」的假象。

## 可观测

- **不给既有的 LLM 调用计数加模型标签**（高基数纪律）；
- 降级用**独立的低基数计数器**（`reason` ∈ {timeout, rate_limited, server_error}）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterator

from app.core.logging_setup import get_logger
from app.services.llm import LLM, LLMError, TokenUsage

logger = get_logger(__name__)

#: 降级原因（低基数枚举，指标 label 用）
REASON_TIMEOUT = "timeout"
REASON_RATE_LIMITED = "rate_limited"
REASON_SERVER_ERROR = "server_error"


def _reason_of(exc: LLMError) -> str:
    """把错误码归到三个低基数原因之一。"""
    code = int(exc.code)
    if code == 4003:  # GENERATION_TIMEOUT
        return REASON_TIMEOUT
    if code == 6101:  # RATE_LIMITED
        return REASON_RATE_LIMITED
    return REASON_SERVER_ERROR


@dataclass(frozen=True)
class ModelPolicy:
    """一个任务档位的模型候选链。

    `primary` 失败且**可降级**时，按 `fallbacks` 的顺序依次尝试；
    `max_attempts` 限制总尝试次数（默认 2：主模型 + 一个备选）。
    """

    primary: str
    fallbacks: tuple[str, ...] = ()
    max_attempts: int = 2

    def candidates(self) -> list[str]:
        chain = [self.primary, *self.fallbacks]
        return chain[: max(1, self.max_attempts)]


@dataclass
class RouteResult:
    """一次路由调用的结果（含**实际使用的模型**与降级标记）。"""

    text: str
    model: str
    degraded: bool = False
    reason: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)


class LLMRouter:
    """按候选链调用模型，主模型失败且可降级时落到备选。

    `factory`：按模型名**重建实例**（`Callable[[str], LLM]`）——
    避免运行期改共享实例的状态（并发下会串）。
    """

    def __init__(
        self,
        factory: Callable[[str], LLM],
        policy: ModelPolicy,
        on_degrade: Callable[[str], None] | None = None,
    ) -> None:
        self._factory = factory
        self.policy = policy
        self._on_degrade = on_degrade

    #: 最近一次路由的结果（供上层读取「实际使用的模型 / 是否降级」）
    last_result: RouteResult | None = None

    def generate(
        self,
        prompt: str,
        deadline=None,
        max_tokens: int | None = None,
    ) -> str:
        """调用候选链；返回**文本**（与 `LLM` 协议一致）。

        路由元信息（实际模型 / 是否降级 / 原因）放在 `last_result`——
        路由器要能**顶替** `LLM` 的位置（调用方不应知道底下有路由）。
        """
        candidates = self.policy.candidates()
        last_error: LLMError | None = None

        for index, model in enumerate(candidates):
            is_primary = index == 0
            try:
                llm = self._factory(model)
                try:
                    text = llm.generate(prompt, deadline=deadline, max_tokens=max_tokens)
                except TypeError:
                    # 适配器不支持 `max_tokens`（第三方实现 / 测试替身）→ 退回旧调用面
                    text = llm.generate(prompt)
            except LLMError as exc:
                last_error = exc
                # 纪律 1 / 2：不可降级的失败（鉴权 / 参数错 / 熔断）直接上抛
                if not exc.degradable:
                    raise
                if index == len(candidates) - 1:
                    break
                reason = _reason_of(exc)
                logger.warning(
                    "主模型 %s 失败（%s），降级到备选 %s", model, reason, candidates[index + 1]
                )
                if self._on_degrade is not None:
                    try:
                        self._on_degrade(reason)
                    except Exception:  # noqa: BLE001 - 指标失败不影响降级
                        pass
                self.last_result = RouteResult(
                    text="", model=model, degraded=True, reason=reason,
                )
                continue

            degraded = not is_primary
            self.last_result = RouteResult(
                text=text,
                model=model,
                degraded=degraded,
                reason=_reason_of(last_error) if degraded and last_error else "",
                usage=getattr(llm, "last_usage", TokenUsage()) or TokenUsage(),
            )
            return text

        # 纪律 3：全部候选失败 → 抛原异常（不吞、不假装成功）
        assert last_error is not None
        raise last_error

    # ---- F5：推荐追问 / 会话标题（走主模型，不参与降级）----
    # 降级只针对「回答问题」这条路径；追问与命名是锦上添花，
    # 主模型失败时它们失败即可（上层已各自有降级）。
    def generate_followups(self, answer: str, n: int = 3) -> list[str]:
        # 破环：llm 反向依赖路由器的类型（生成追问要拿到 LLM 实例）
        from app.services.llm import generate_followups

        return generate_followups(self._factory(self.policy.primary), answer, n)

    def generate_title(self, question: str, answer: str) -> str:
        # 破环：llm 反向依赖路由器的类型（生成标题要拿到 LLM 实例）
        from app.services.llm import generate_title

        return generate_title(self._factory(self.policy.primary), question, answer)

    def stream(
        self, prompt: str, deadline=None, max_tokens: int | None = None
    ) -> Iterator[str]:
        """流式：降级逻辑与 `generate` 一致（但只返回文本流，不带路由元信息）。

        流式的降级必须**在第一次产出前**决定——已经吐出去的 token 不能撤回，
        所以这里只在「还没产出任何内容」时才允许换模型。
        """
        candidates = self.policy.candidates()
        last_error: LLMError | None = None
        for index, model in enumerate(candidates):
            produced = False
            try:
                llm = self._factory(model)
                try:
                    for piece in llm.stream(prompt, deadline=deadline, max_tokens=max_tokens):
                        produced = True
                        yield piece
                except TypeError:
                    # 同上：适配器不支持 `max_tokens` → 退回旧调用面
                    for piece in llm.stream(prompt):
                        produced = True
                        yield piece
                return
            except LLMError as exc:
                last_error = exc
                if produced or not exc.degradable or index == len(candidates) - 1:
                    raise
                logger.warning("主模型 %s 流式失败，降级到备选 %s", model, candidates[index + 1])
                continue
        assert last_error is not None
        raise last_error
