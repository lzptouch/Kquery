"""LLM 抽象（依赖注入 / 模型统一走千问平台）。

- 生产实现：千问平台 qwen-plus（temperature=0，流式增量输出）；
- 测试实现：MockLLM，基于证据编号生成确定性的带引用答案。

云端三类失败模式：限流（退避重试）/ 超时（请求级超时，不返回半截答案）/
配额不足（直接失败，属运维事件）。
"""
import json
import re
import time
from dataclasses import dataclass
from typing import Iterator, Protocol

from app.core.deadline import Deadline
from app.domain.constants import NO_INFO_ANSWER
from app.core.envelope import ErrCode
from app.services.upstream import CircuitOpenError, bulkhead
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

#: Mock 模式的确定性追问（F5）：供测试断言固定输出
_MOCK_FOLLOWUPS = [
    "这个结论的依据是什么？",
    "还有哪些相关的规定？",
    "在什么情况下不适用？",
]
#: Mock 模式的确定性会话标题（F5）
_MOCK_TITLE = "知识库问答"


class LLMError(Exception):
    """LLM 调用失败，携带错误码供上层映射统一信封。

    ## `retryable` / `degradable` 这两个标记（`LM-01`）

    之前所有失败都长一个样，上层无法判断「换个模型有没有意义」——
    结果要么直接失败，要么退到通用知识兜底（内容降级，用户看不出区别）。

    | 情况 | retryable | degradable | 说明 |
    |---|---|---|---|
    | 429 / 配额耗尽 | ✅ | ✅ | 换个模型可能绕开（不同模型不同配额池） |
    | 超时 | ✅ | ✅ | 慢的模型换快的 |
    | 5xx | ✅ | ✅ | 平台侧瞬时故障 |
    | 401 / 403（鉴权） | ❌ | ❌ | 换个模型也是同一个 Key——**降级只会掩盖配置错误** |
    | 400（参数错） | ❌ | ❌ | 请求本身不合法，换模型也错 |
    | 熔断打开 | ❌ | ❌ | **整条上游通道**病了，换同平台的模型无济于事 |
    """

    def __init__(
        self,
        code: ErrCode,
        message: str,
        *,
        retryable: bool = False,
        degradable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        # `degradable` 默认跟随 `retryable`：可重试的失败才可能换个模型成功
        self.degradable = retryable if degradable is None else degradable

@dataclass
class TokenUsage:
    """单次调用的 token 用量。

    `estimated=True` 表示供应商未返回 usage、按字符数估算——**必须显式标注**，
    否则成本看板会把估算值当真值做归集与告警。
    """
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = True

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    @classmethod
    def estimate(cls, prompt: str, text: str = "") -> "TokenUsage":
        """字符数粗估：中英混排按 ~2 字符/token 折中，宁可高估也不低估成本。"""
        return cls(
            input_tokens=max(1, len(prompt) // 2),
            output_tokens=max(0, len(text) // 2),
            estimated=True,
        )


class LLM(Protocol):
    def generate(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> str: ...

    # 注：`stream()` 是**协议声明**，但当前生产链路并未真正增量输出——
    # 线性路径全程阻塞生成，Agent 路径的流式被缓冲后按终稿切片重放
    # （`services/pipeline.py` 的 `_final` 分支）。真流式（`C-13`）现状见 `doc/analysis/04-问答链路.md`。
    def stream(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]: ...

    # 最近一次调用的 token 用量。刻意**不改 generate/stream 的返回值**：
    # 那是 6 处调用方的公共契约，改返回值会让所有适配器一起返工；改为在实例上
    # 暴露只读属性，调用方用 getattr(llm, "last_usage", None) 兜底即可。
    #
    # `deadline` 是**新增的可选关键字参数**：不传就是旧行为（无上限），
    # 传了就按剩余预算裁剪超时与重试。默认参数保证 6 处老调用方零改动。
    last_usage: TokenUsage


class QwenLLM:
    """千问平台生成模型。切换档位（qwen-plus/qwen-max/qwen-turbo）只改模型名。"""

    def __init__(
        self,
        api_key: str,
        model: str = "qwen-plus",
        temperature: float = 0.0,
        max_retries: int = 3,
        timeout_s: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.temperature = temperature
        self._max_retries = max_retries
        self._timeout_s = timeout_s
        # 最近一次调用的 token 用量，由 _call_stream 在流结束时刷新
        self._last_usage = TokenUsage()

    @property
    def last_usage(self) -> TokenUsage:
        return self._last_usage

    @staticmethod
    def _extract_usage(resp, prompt: str, produced: str) -> TokenUsage:
        """从供应商响应读 usage；流式响应只在**末块**携带，读不到则按字符估算。"""
        raw = getattr(resp, "usage", None)
        if raw:
            data = raw if isinstance(raw, dict) else {
                k: getattr(raw, k, None) for k in ("prompt_tokens", "completion_tokens")
            }
            try:
                inp = int(data.get("prompt_tokens") or 0)
                out = int(data.get("completion_tokens") or 0)
            except (TypeError, ValueError):
                inp = out = 0
            if inp or out:
                return TokenUsage(input_tokens=inp, output_tokens=out, estimated=False)
        return TokenUsage.estimate(prompt, produced)

    def _call_stream(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        from dashscope import Generation

        last_error: Exception | None = None
        produced: list[str] = []
        self._last_usage = TokenUsage.estimate(prompt)
        for attempt in range(self._max_retries):
            # 本次尝试的超时：取"配置上限"与"剩余预算"的较小者。
            # 预算耗尽时直接失败，不再发起一次注定超时的调用。
            attempt_timeout = self._timeout_s
            if deadline is not None:
                remaining_s = deadline.remaining_ms / 1000
                if remaining_s <= 0:
                    raise LLMError(
                        ErrCode.GENERATION_TIMEOUT, "请求已超出时间预算，未再发起模型调用"
                    )
                attempt_timeout = max(0.5, min(self._timeout_s, remaining_s))
            try:
                # 批次 2：上游隔离仓。许可覆盖整段流式消费——并发控制的对象是
                # "在飞的上游调用"，不是单次 HTTP 往返。熔断打开时快速失败不占线程。
                with bulkhead("llm").acquire(timeout_s=attempt_timeout):
                    responses = Generation.call(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=self.temperature,
                        result_format="message",
                        stream=True,
                        incremental_output=True,
                        api_key=self._api_key,
                        timeout=attempt_timeout,
                        # `ST-04` 输出上限：**None 时不下发该参数**（与上游默认一致）
                        **({"max_tokens": int(max_tokens)} if max_tokens else {}),
                    )
                    for resp in responses:
                        if resp.status_code != 200:
                            self._raise_for(resp.status_code, resp.message)
                        chunk = resp.output.choices[0].message.content
                        if chunk:
                            produced.append(chunk)
                            yield chunk
                # 流式末块才带 usage；读不到则用累计文本估算
                self._last_usage = self._extract_usage(resp, prompt, "".join(produced))
                return
            except LLMError:
                raise
            except CircuitOpenError as exc:
                # 熔断打开不重试、**也不降级**（`LM-01`）：熔断说明**整条上游通道**病了，
                # 换同平台的另一个模型无济于事（同一个 Key、同一个域名）。
                raise LLMError(
                    ErrCode.DEPENDENCY_UNAVAILABLE,
                    f"上游熔断（{exc.upstream}），请稍后重试",
                    retryable=False,
                    degradable=False,
                ) from exc
            except Exception as exc:  # noqa: BLE001 - 网络类异常走重试
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                wait = 2**attempt
                if deadline is not None and not deadline.can_wait(wait, reserve_s=1.0):
                    # 剩余预算不够"再等一次并且那次跑得起来" → 不做注定超时的重试
                    logger.warning(
                        "LLM 调用失败且剩余预算不足，放弃重试（%d/%d）: %s",
                        attempt + 1, self._max_retries, exc,
                    )
                    break
                logger.warning("LLM 调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, exc)
                if deadline is not None:
                    deadline.wait_s(wait)
                else:
                    time.sleep(wait)
        raise LLMError(ErrCode.GENERATION_FAILED, f"生成失败: {last_error}")

    @staticmethod
    def _raise_for(status_code: int, message: str) -> None:
        """把供应商状态码翻译成错误码 + **可重试 / 可降级标记**（`LM-01`）。

        鉴权（401/403）与参数错（400）**不可降级**：同一个 Key 换个模型也一样失败，
        降级只会把「配置错了」伪装成「模型不行」。
        """
        text = str(message)
        if status_code == 429 or "Throttl" in text:
            raise LLMError(ErrCode.RATE_LIMITED, "平台限流，请稍后重试", retryable=True)
        if "quota" in text.lower() or "Allocation" in text:
            # 平台侧额度耗尽与限流同源，统一按限流语义返回
            raise LLMError(ErrCode.RATE_LIMITED, "平台额度不足，请稍后重试", retryable=True)
        if "timeout" in text.lower():
            raise LLMError(ErrCode.GENERATION_TIMEOUT, "生成超时，请重试", retryable=True)
        if status_code in (401, 403):
            raise LLMError(
                ErrCode.GENERATION_FAILED, f"模型服务鉴权失败（HTTP {status_code}）", retryable=False
            )
        if status_code == 400:
            raise LLMError(
                ErrCode.GENERATION_FAILED, "模型服务拒绝了请求（HTTP 400）", retryable=False
            )
        retryable = status_code >= 500
        raise LLMError(
            ErrCode.GENERATION_FAILED, f"生成失败: {message}", retryable=retryable
        )

    def generate(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self._call_stream(prompt, deadline=deadline, max_tokens=max_tokens))

    def stream(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        return self._call_stream(prompt, deadline=deadline, max_tokens=max_tokens)

    # ---- F5：推荐追问 / 会话标题 ----

    def generate_followups(self, answer: str, n: int = 3) -> list[str]:
        return generate_followups(self, answer, n)

    def generate_title(self, question: str, answer: str) -> str:
        return generate_title(self, question, answer)


class DeepSeekLLM:
    """DeepSeek 生成模型（OpenAI 兼容的 `/chat/completions`）。

    ## 与 `QwenLLM` 的差别

    只有**传输层**不同：请求体是 OpenAI 兼容格式
    （`{"model","messages","temperature","stream"}`），
    响应增量在 `choices[0].delta.content`。
    **重试 / 超时 / deadline / 隔离仓 / token 用量口径完全一致**——
    这几件事的行为如果按供应商各写一套，换供应商就会换行为。

    ## DeepSeek 不提供什么

    DeepSeek 官方 API **只有对话补全**：没有 embeddings 端点、没有 rerank 端点。
    「用 DeepSeek 做向量 / 重排」在直连 DeepSeek 时不可行——
    详见 `services/openai_compat.py` 的模块说明。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "deepseek-chat",
        base_url: str = "https://api.deepseek.com",
        temperature: float = 0.0,
        max_retries: int = 3,
        timeout_s: float = 60.0,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._base_url = base_url.rstrip("/")
        self.temperature = temperature
        self._max_retries = max(1, int(max_retries))
        self._timeout_s = timeout_s
        self._last_usage = TokenUsage()
        #: 上游最后给出的 `finish_reason`（`length` = 被输出上限截断）
        self._finish_reason: str = ""

    @property
    def last_usage(self) -> TokenUsage:
        return self._last_usage

    @property
    def last_finish_reason(self) -> str:
        """最后一次生成的 `finish_reason`（空串 = 上游没给）。"""
        return self._finish_reason

    @property
    def _endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _extra_payload(self) -> dict:
        """provider 私有的请求字段（基类没有；子类按需覆盖）。"""
        return {}

    def _call_stream(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        # 按需载入：兼容端点客户端只在真正调用时才需要（DashScope 路径不必付导入成本）
        from app.services.openai_compat import OpenAICompatError, stream_lines

        last_error: Exception | None = None
        produced: list[str] = []
        self._last_usage = TokenUsage.estimate(prompt)
        self._finish_reason = ""
        for attempt in range(self._max_retries):
            attempt_timeout = self._timeout_s
            if deadline is not None:
                remaining_s = deadline.remaining_ms / 1000
                if remaining_s <= 0:
                    raise LLMError(
                        ErrCode.GENERATION_TIMEOUT, "请求已超出时间预算，未再发起模型调用"
                    )
                attempt_timeout = max(0.5, min(self._timeout_s, remaining_s))
            try:
                with bulkhead("llm").acquire(timeout_s=attempt_timeout):
                    payload = {
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": self.temperature,
                        "stream": True,
                        # `ST-04` 输出上限（None 时不下发，保持与上游默认一致）
                        **self._output_budget(max_tokens),
                        # provider 私有请求字段（默认空；子类覆盖，如智谱关思考）
                        **self._extra_payload(),
                    }
                    for raw in stream_lines(
                        self._endpoint, payload, self._api_key, timeout_s=attempt_timeout
                    ):
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue  # 心跳 / 非 JSON 片段：跳过
                        self._raise_provider_error(chunk)
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        delta = (choices[0] or {}).get("delta") or {}
                        # 推理模型会把思考过程放在 `reasoning_content`（MiniMax M3 系）
                        # 或独立 delta 类型里：**不能当正文**，否则答案里混进分析过程。
                        if delta.get("reasoning_content"):
                            continue
                        piece = delta.get("content")
                        if piece:
                            produced.append(piece)
                            yield piece
                        finish = (choices[0] or {}).get("finish_reason")
                        if finish:
                            self._finish_reason = str(finish)
                text = "".join(produced)
                self._last_usage = TokenUsage.estimate(prompt, text)
                self._assert_payload(text)
                return
            except LLMError:
                # 上游把业务错误塞在 HTTP 200 里（见 `_raise_provider_error`）：
                # 已经是归一后的错误码，重试与降级口径由抛出方决定，不能再被下面的
                # 通用捕获当「网络抖动」重试三次。
                raise
            except OpenAICompatError as exc:
                last_error = exc
                if exc.status_code in (401, 403):
                    # 凭据错误：重试没有意义，**也不可降级**（同一个 Key 换个模型一样失败）
                    raise LLMError(
                        ErrCode.GENERATION_FAILED,
                        f"模型服务鉴权失败（HTTP {exc.status_code}）",
                        retryable=False,
                        degradable=False,
                    ) from exc
                if exc.status_code == 429:
                    raise LLMError(ErrCode.RATE_LIMITED, "平台限流，请稍后重试", retryable=True) from exc
                if exc.status_code == 504:
                    raise LLMError(ErrCode.GENERATION_TIMEOUT, "生成超时，请重试", retryable=True) from exc
                if attempt == self._max_retries - 1:
                    break
                wait = 2**attempt
                logger.warning(
                    "模型调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, exc
                )
                time.sleep(wait)
            except Exception as exc:  # noqa: BLE001 - 统一转为重试或上抛
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                wait = 2**attempt
                logger.warning(
                    "模型调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, exc
                )
                time.sleep(wait)
        raise LLMError(ErrCode.GENERATION_FAILED, f"生成失败: {last_error}", retryable=True)

    def generate(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> str:
        return "".join(self._call_stream(prompt, deadline=deadline, max_tokens=max_tokens))

    def stream(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        return self._call_stream(prompt, deadline=deadline, max_tokens=max_tokens)

    # ---- F5：推荐追问 / 会话标题 ----

    def generate_followups(self, answer: str, n: int = 3) -> list[str]:
        return generate_followups(self, answer, n)

    def generate_title(self, question: str, answer: str) -> str:
        return generate_title(self, question, answer)

    def _output_budget(self, max_tokens: int | None) -> dict:
        """把 `ST-04` 的输出上限翻译成该 provider 的请求字段。

        默认就是 `max_tokens`；推理模型需要额外余量时子类覆盖
        （思考 token 同样计入上限，见 `MiniMaxLLM._output_budget`）。
        """
        if not max_tokens:
            return {}
        return {"max_tokens": int(max_tokens)}

    def _assert_payload(self, text: str) -> None:
        """生成结果的**内容判据**（默认接受空串）。

        子类在「空结果其实代表失败」时覆盖它——比让空答案一路走到
        引用校验、再变成一条「模型没话说」的日志要好排查得多。
        """
        return None

    def _raise_provider_error(self, chunk: dict) -> None:
        """上游把业务错误塞在**HTTP 200 的响应体**里时的检查点（默认无操作）。

        OpenAI 兼容协议靠 HTTP 状态码表意，但不是所有平台都照做。
        子类在这里把业务错误翻译成 `LLMError`；**不检查的后果是把错误当成功**——
        空 delta 被当成「模型没话说」，用户得到一个既没内容也没报错的答案。
        """
        return None


class MiniMaxLLM(DeepSeekLLM):
    """MiniMax 生成模型（OpenAI 兼容的 `/chat/completions`；M3 系列**原生多模态**）。

    传输层与 `DeepSeekLLM` 完全相同，所以直接继承——两套实现会让
    「重试 / 超时 / deadline / 隔离仓 / usage」按供应商漂移。

    ## 唯一的协议差异：错误表意位置

    MiniMax 用 **HTTP 200 + `base_resp.status_code`** 表意业务错误
    （`0` 才是成功）。因此必须覆盖 `_raise_provider_error`，把它翻译成与其他
    provider 一致的 `LLMError`——错误码、可重试、可降级这三件套是上层的熔断与
    降级依据，不能按供应商各说各话。

    ## 多模态

    M3 系列接受 OpenAI 风格的 `image_url` content part，`image_url` 用 `data:` URL
    （base64）传图，`services/multimodal.py` 的直答器负责拼装，本类只做**纯文本**补全。

    ## 推理模型：思考 token 也算进输出上限

    `MiniMax-M3.1-Flash` **恒开思考**（官方文档：只能用 `reasoning_effort` 调深度，
    不能像 `MiniMax-M3` 那样传 `thinking: {"type": "disabled"}` 关掉），而思考 token
    **计入 `max_tokens` 且按输出价计费**。`ST-04` 的输出上限设得小时，模型会把预算
    全烧在思考上、`finish_reason=length`、正文为空——看起来像「模型没话说」。
    因此在 `_output_budget` 里给上限加推理余量，`_assert_payload` 把空正文判成失败。
    """

    #: 业务错误码 → (错误码, 可重试, 可降级)。
    #: 只收录**已确认**的码；没收录的非零码一律按「确定性业务拒绝」处理，
    #: 不做重试（业务错误重试三次只是把同一个拒绝问三遍）。
    _BUSINESS_ERRORS: dict[int, tuple[str, bool, bool]] = {
        1001: (ErrCode.GENERATION_TIMEOUT, True, True),    # 请求超时
        1002: (ErrCode.RATE_LIMITED, True, True),          # 触发限流
        1004: (ErrCode.GENERATION_FAILED, False, False),   # 未授权 / Token 不匹配
        1008: (ErrCode.GENERATION_FAILED, False, False),   # 余额不足
    }

    def __init__(
        self,
        api_key: str,
        model: str = "MiniMax-M3.1-Flash",
        base_url: str = "https://api.minimax.chat/v1",
        temperature: float = 0.0,
        max_retries: int = 3,
        timeout_s: float = 60.0,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_retries=max_retries,
            timeout_s=timeout_s,
        )

    #: 推理余量：正文预算之外额外给 1024 token 的思考空间。
    #: 依据是「思考 token 计入上限」（见类说明）——不改上限等于让小上限的问答
    #: 随机变成空答案，而**空答案的报错位置离真正原因很远**（引用校验那一步）。
    _REASONING_HEADROOM = 1024

    def _output_budget(self, max_tokens: int | None) -> dict:
        """给输出上限加上推理余量（M3.1-Flash 关不掉思考）。

        用 `max_tokens` 而不是 `max_completion_tokens`：两者语义相同
        （都含思考），同时下发反而可能被上游判为参数冲突。
        """
        if not max_tokens:
            return {}
        return {"max_tokens": int(max_tokens) + self._REASONING_HEADROOM}

    def _assert_payload(self, text: str) -> None:
        """空正文 = 失败（不是「模型没话说」）。

        `MiniMax-M3.1-Flash` 恒开思考：`max_tokens` 被思考烧完时正文为空、
        `finish_reason=length`。把这种情况报成 `GENERATION_FAILED`（不重试——
        同样预算重试三次只会烧三次思考 token），并给出可执行的修法。
        """
        if text.strip():
            if self._finish_reason == "length":
                logger.warning(
                    "MiniMax 输出被截断（finish_reason=length，len=%d）：思考 token 占用上限，"
                    "答案可能不完整", len(text),
                )
            return
        if self._finish_reason == "length":
            raise LLMError(
                ErrCode.GENERATION_FAILED,
                "MiniMax 未返回正文：思考过程耗尽了 max_tokens（finish_reason=length）",
                retryable=False,
                degradable=True,
            )
        raise LLMError(
            ErrCode.GENERATION_FAILED,
            "MiniMax 返回空正文（finish_reason=%s）" % (self._finish_reason or "未知"),
            retryable=False,
            degradable=True,
        )

    def _raise_provider_error(self, chunk: dict) -> None:
        base = chunk.get("base_resp") or {}
        try:
            code = int(base.get("status_code") or 0)
        except (TypeError, ValueError):
            return
        if code == 0:
            return
        msg = str(base.get("status_msg") or "").strip()
        err_code, retryable, degradable = self._BUSINESS_ERRORS.get(
            code, (ErrCode.GENERATION_FAILED, False, False)
        )
        raise LLMError(
            err_code,
            f"MiniMax 返回业务错误 {code}: {msg or '（无描述）'}",
            retryable=retryable,
            degradable=degradable,
        )


class MockLLM:
    """确定性假模型：从 prompt 中解析证据编号，生成带引性的可预期答案。"""

    # 与 pipeline 的拒答检测口径**同一个常量**（此前各写一份字面量，
    # 一旦漂移，mock 的拒答就检测不出来）
    _NO_INFO = NO_INFO_ANSWER

    def __init__(self) -> None:
        # Mock 模式也要有 usage，否则配额结算路径在测试里走不通
        self._last_usage = TokenUsage()

    @staticmethod
    def _parse(prompt: str) -> tuple[str, list[tuple[str, str]]]:
        question = ""
        m = re.search(r"用户问题：(.+?)(?:\n|$)", prompt)
        if m:
            question = m.group(1).strip()
        evidence = re.findall(r"\[#(\d)\]（来源：(.+?)）\n(.*?)(?=\n\[#|\n---|\Z)", prompt, re.S)
        return question, [(num, body.strip()) for num, _src, body in evidence]

    # 问句中不参与匹配的虚词
    _STOPWORDS = {
        "的", "是", "吗", "呢", "了", "请", "问", "告诉", "我", "你",
        "什么", "怎么", "怎样", "如何", "哪些", "哪个", "多少", "几", "是否",
    }

    def _question_keywords(self, question: str) -> set[str]:
        """问句关键词：长度 ≥2 的词/二元组与英文数字词，去掉虚词。"""
        # 破环：embeddings 依赖 llm 的分词器接口
        from app.services.embeddings import _tokenize

        tokens = _tokenize(question)
        return {
            t
            for t in tokens
            if (len(t) >= 2 or t.isascii()) and t not in self._STOPWORDS and t.isalnum()
        }

    # ---- Query 预处理类提示词的确定性应答 ----

    _GREETINGS = ("你好", "您好", "在吗", "你是谁", "谢谢", "再见", "hi", "hello")

    def _mock_intent(self, prompt: str) -> str:
        m = re.search(r"用户问题：(.+?)(?:\n|$)", prompt)
        q = (m.group(1).strip() if m else "").lower()
        if any(g in q for g in self._GREETINGS) and len(q) < 20:
            payload = {"intent": "chitchat", "needs_retrieval": False, "confidence": 0.95, "reasoning": "mock 规则：命中闲聊问候模式"}
        elif any(k in q for k in ("对比", "哪个好", "区别", "差异")):
            payload = {"intent": "compare", "needs_retrieval": True, "confidence": 0.9, "reasoning": "mock 规则：含对比关键词"}
        elif any(k in q for k in ("总结", "讲了什么", "概括", "综述")):
            payload = {"intent": "summarize", "needs_retrieval": True, "confidence": 0.9, "reasoning": "mock 规则：含总结关键词"}
        elif any(k in q for k in ("平均", "统计", "总数", "占比")):
            payload = {"intent": "calc", "needs_retrieval": True, "confidence": 0.85, "reasoning": "mock 规则：含统计关键词"}
        else:
            payload = {"intent": "fact_qa", "needs_retrieval": True, "confidence": 0.9, "reasoning": "mock 规则：默认事实问答"}
        return json.dumps(payload, ensure_ascii=False)

    def _mock_fusion(self, prompt: str) -> str:
        m = re.search(r"用户问题：(.+?)(?:\n|$)", prompt)
        q = m.group(1).strip() if m else "查询"
        return json.dumps([q, f"请问{q}", f"关于{q}的详细说明"], ensure_ascii=False)

    def _mock_hyde(self, prompt: str) -> str:
        m = re.search(r"问题：(.+?)(?:\n|$)", prompt)
        q = m.group(1).strip() if m else "该主题"
        return f"关于{q}，相关知识库文档通常说明：{q}的标准定义、关键参数与使用注意事项。本文档给出权威解答。"

    def _mock_stepback(self, prompt: str) -> str:
        m = re.search(r"具体问题：(.+?)(?:\n|$)", prompt)
        q = m.group(1).strip() if m else ""
        abstract = re.sub(r"[A-Z]{1,4}[-_]?\d{3,}", "该类产品", q)
        return abstract or "该类产品的通用原则是什么？"

    def _mock_decompose(self, prompt: str) -> str:
        m = re.search(r"复合问题：(.+?)(?:\n|$)", prompt)
        q = m.group(1).strip() if m else ""
        parts = [p for p in re.split(r"和|与|以及|，|、", q) if p.strip()]
        items = [{"text": p.strip(), "expected_source": "vector"} for p in parts[:4]] or [
            {"text": q, "expected_source": "vector"}
        ]
        return json.dumps(items, ensure_ascii=False)

    def _answer(self, prompt: str) -> str:
        # 破环：embeddings 依赖 llm 的分词器接口
        from app.services.embeddings import _tokenize

        # F5：追问 / 标题类提示词的确定性应答
        if "用户最可能继续追问的问题" in prompt:
            return json.dumps(_MOCK_FOLLOWUPS, ensure_ascii=False)
        if "为这次对话起一个不超过" in prompt:
            return _MOCK_TITLE

        # 预处理类提示词优先按类型分发
        if "你是意图分类器" in prompt:
            return self._mock_intent(prompt)
        if "你是事实校验器" in prompt:
            return json.dumps(
                {"is_hallucination": False, "unsupported_claims": [], "reasoning": "mock 校验：声明均有据"},
                ensure_ascii=False,
            )
        if "精炼检索查询" in prompt:
            m = re.search(r"原始问题：(.+?)(?:\n|$)", prompt)
            q = m.group(1).strip() if m else "查询"
            return f"{q}（规范术语与通用原则）"
        if "概括以下内容" in prompt:
            # RAPTOR 摘要：抽取式概要（首个完整句 + 主题标记）
            body = prompt.split("概括以下内容", 1)[-1].strip(" ：:\n")
            first = re.split(r"(?<=[。！？!?；;\n])", body.strip())
            head = next((s.strip() for s in first if len(s.strip()) >= 6), body[:80])
            return f"本节要点：{head[:80]}"
        if "语义等价但措辞不同" in prompt:
            return self._mock_fusion(prompt)
        if "假设性回答" in prompt:
            return self._mock_hyde(prompt)
        if "抽象成一个更通用" in prompt:
            return self._mock_stepback(prompt)
        if "拆解为 2~4 个可独立检索" in prompt:
            return self._mock_decompose(prompt)
        # 通用知识兜底提示词的确定性应答：不含 [n] 引用、不以拒答话术开头
        # （否则 parametric 路径在测试里永远走不到，退化为"兜底失败回退拒答"）
        if "用户的问题在知识库中没有检索到相关内容" in prompt:
            m = re.search(r"用户问题：(.+?)(?:\n|$)", prompt)
            q = m.group(1).strip() if m else "该问题"
            return (
                "以下回答基于大模型通用知识，知识库中未收录相关内容。"
                f"{q}的通用说明：这是模型基于自身知识生成的兜底回答，仅供测试参考。"
            )

        question, evidence = self._parse(prompt)
        if not evidence:
            return self._NO_INFO

        keywords = self._question_keywords(question)
        # 按"与问句的关键词重合度"给证据中的每个句子打分，抽出最相关的句子
        scored: list[tuple[int, str, str]] = []  # (score, num, sentence)
        for num, body in evidence:
            for sentence in re.split(r"(?<=[。！？!?；;\n])", body):
                sentence = sentence.strip()
                if len(sentence) < 6 or sentence.startswith(("#", "问：")):
                    continue
                s_tokens = {t for t in _tokenize(sentence) if len(t) >= 2 or t.isascii()}
                score = len(keywords & s_tokens)
                if score > 0:
                    scored.append((score, num, sentence))
        scored.sort(key=lambda x: -x[0])

        picked: list[tuple[str, str]] = []
        seen: set[str] = set()
        for score, num, sentence in scored:
            if sentence[:30] in seen:
                continue
            seen.add(sentence[:30])
            picked.append((num, sentence))
            if len(picked) >= 3:
                break
        if not picked:
            # 兜底：取第一条证据的首个完整句
            num, body = evidence[0]
            first = re.split(r"(?<=[。！？!?；;\n])", body.strip())[0].strip()
            picked = [(num, first[:120])]

        # 合成直接答案：结论句在前，引用编号紧随其后
        answer_lines = [f"{sentence}[#{num}]" for num, sentence in picked]
        cited_nums = list(dict.fromkeys(num for num, _ in picked))
        answer_lines.append("")
        answer_lines.append("引用片段编号：" + "、".join(f"[#{n}]" for n in cited_nums))
        return "\n".join(answer_lines)

    def generate(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> str:
        # `max_tokens` **存在但忽略**（`ST-04` 兼容纪律）：
        # mock 不生成真实 token，截断会破坏既有断言。
        text = self._answer(prompt)
        self._last_usage = TokenUsage.estimate(prompt, text)
        return text

    # ---- F5：确定性追问与标题（供测试断言固定输出）----

    def generate_followups(self, answer: str, n: int = 3) -> list[str]:
        return _MOCK_FOLLOWUPS[:n]

    def generate_title(self, question: str, answer: str) -> str:
        return _MOCK_TITLE

    def stream(
        self,
        prompt: str,
        deadline: "Deadline | None" = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        # 同上：`max_tokens` 存在但忽略
        text = self._answer(prompt)
        self._last_usage = TokenUsage.estimate(prompt, text)
        # 按小片段吐出，模拟流式
        step = 12
        for i in range(0, len(text), step):
            yield text[i : i + step]

    @property
    def last_usage(self) -> TokenUsage:
        return self._last_usage


class ZhipuLLM(DeepSeekLLM):
    """智谱开放平台（`open.bigmodel.cn`）生成模型。

    ## 为什么继承 `DeepSeekLLM`

    智谱的 `/api/paas/v4/chat/completions` 与 DeepSeek 的请求/响应形状完全相同，
    所以只覆盖**请求体增量**，重试 / 超时 / deadline / 隔离仓 / usage 口径全部继承——
    这几件事一按供应商分叉，换模型就变成换行为。

    ## 唯一差异：glm-4.5 系默认开思考

    实测：`glm-4.6` / `glm-4.5` / `glm-4.5-air` / `glm-4.5-flash`
    在 `max_tokens=120` 时思考 token 把预算吃光，`finish_reason=length`、正文为空。
    RAG 场景会拿到**空答案但不报错**，属于最难排查的失败模式，
    所以默认下发 `thinking: {"type": "disabled"}`；需要推理链时才显式打开
    （`KQUERY_ZHIPU_THINKING_ENABLED=1`）。

    `glm-4-*` 老档位（`glm-4-air` / `glm-4-flash`）本来就不思考，
    下发该字段也兼容（实测 200），因此不必按模型名分叉。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "glm-4.6",
        base_url: str = "https://open.bigmodel.cn/api/paas/v4",
        temperature: float = 0.0,
        max_retries: int = 3,
        timeout_s: float = 60.0,
        thinking_enabled: bool = False,
    ) -> None:
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=temperature,
            max_retries=max_retries,
            timeout_s=timeout_s,
        )
        self._thinking_enabled = bool(thinking_enabled)

    def _extra_payload(self) -> dict:
        return {"thinking": {"type": "enabled" if self._thinking_enabled else "disabled"}}


def build_llm(
    provider: str,
    api_key: str,
    model: str,
    temperature: float,
    max_retries: int,
    timeout_s: float,
    base_url: str = "",
    thinking_enabled: bool = False,
) -> LLM:
    """按 provider 构造生成模型。

    `provider` 取值：`dashscope`（千问）/ `deepseek` / `minimax` /
    `zhipu`（后三者均为 OpenAI 兼容形状）/ `mock`（确定性假模型）。
    **行为口径（重试 / 超时 / 隔离仓 / usage）全部一致**——
    按供应商各写一套会让「换模型」变成「换行为」。
    """
    if provider == "dashscope":
        return QwenLLM(
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_retries=max_retries,
            timeout_s=timeout_s,
        )
    if provider == "deepseek":
        return DeepSeekLLM(
            api_key=api_key,
            model=model or "deepseek-chat",
            base_url=base_url or "https://api.deepseek.com",
            temperature=temperature,
            max_retries=max_retries,
            timeout_s=timeout_s,
        )
    if provider == "minimax":
        return MiniMaxLLM(
            api_key=api_key,
            model=model or "MiniMax-M3.1-Flash",
            base_url=base_url or "https://api.minimax.chat/v1",
            temperature=temperature,
            max_retries=max_retries,
            timeout_s=timeout_s,
        )
    if provider == "zhipu":
        return ZhipuLLM(
            api_key=api_key,
            model=model or "glm-4.6",
            base_url=base_url or "https://open.bigmodel.cn/api/paas/v4",
            temperature=temperature,
            max_retries=max_retries,
            timeout_s=timeout_s,
            thinking_enabled=thinking_enabled,
        )
    logger.info("使用 MockLLM（未配置 Key 或显式指定 mock）")
    return MockLLM()


# ---------- F5：推荐追问与会话命名 ----------

FOLLOWUP_PROMPT = """根据下面的问答内容，提出 {n} 个用户最可能继续追问的问题。
要求：每行一个，不要编号，不要解释，问题本身要具体。

问答内容：
{answer}
"""

TITLE_PROMPT = """根据下面的问答内容，为这次对话起一个不超过 20 字的中文标题。
只输出标题本身，不要引号，不要解释。

问答内容：
{answer}
"""


def generate_followups(llm, answer: str, n: int = 3) -> list[str]:
    """生成推荐追问。

    **失败必须吞掉**：追问是锦上添花，主答案已经生成好了，
    让追问失败把整次问答变成 500 是本末倒置。
    """
    if not answer or not answer.strip():
        return []
    try:
        raw = llm.generate(FOLLOWUP_PROMPT.format(n=n, answer=answer[:4000]))
    except Exception as exc:  # noqa: BLE001 - 追问失败不影响主答案
        logger.warning("生成推荐追问失败（已忽略）: %s", exc)
        return []
    return _parse_followups(raw, n)


def _parse_followups(raw: str, n: int) -> list[str]:
    """解析追问列表。

    兼容两种返回：纯文本多行、JSON 数组或 {"followups": [...]}。
    解析不出来就返回空列表（宁可没有，不要给用户看半截 JSON）。
    """
    if not raw:
        return []
    text = raw.strip()
    # ① JSON 形式：以 [ 或 { 开头就只按 JSON 解析，解析失败就返回空
    #    （半截 JSON 展示给用户比不展示更糟）
    if text.startswith("[") or text.startswith("{"):
        try:
            data = json.loads(text)
            items = data if isinstance(data, list) else data.get("followups") or data.get("questions") or []
            if isinstance(items, list):
                cleaned = [str(i).strip() for i in items if str(i).strip()]
                if cleaned:
                    return cleaned[:n]
        except (ValueError, AttributeError, TypeError):
            logger.debug("追问返回不是合法 JSON，忽略")
        return []
    # ② 纯文本多行
    items = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*•0123456789. 、").strip()
        if len(line) >= 4:
            items.append(line)
    return items[:n]


def generate_title(llm, question: str, answer: str) -> str:
    """生成会话标题（失败返回空串，调用方保留默认标题）。"""
    content = (question or "")[:200] + "\n" + (answer or "")[:2000]
    if not content.strip():
        return ""
    try:
        raw = llm.generate(TITLE_PROMPT.format(answer=content))
    except Exception as exc:  # noqa: BLE001 - 生成会话标题失败
        logger.warning("生成会话标题失败（已忽略）: %s", exc)
        return ""
    title = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    title = title.strip("\"'“”‘’ ")
    return title[:40]

