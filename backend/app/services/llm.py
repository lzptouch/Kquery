"""LLM 抽象（依赖注入 / 模型统一走千问平台）。

- 生产实现：千问平台 qwen-plus（temperature=0，流式增量输出）；
- 测试实现：MockLLM，基于证据编号生成确定性的带引用答案。

云端三类失败模式：限流（退避重试）/ 超时（请求级超时，不返回半截答案）/
配额不足（直接失败，属运维事件）。
"""
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Iterator, Protocol

from app.core.envelope import ErrCode
from app.services.upstream import CircuitOpenError, bulkhead

logger = logging.getLogger(__name__)

#: Mock 模式的确定性追问（F5）：供测试断言固定输出
_MOCK_FOLLOWUPS = [
    "这个结论的依据是什么？",
    "还有哪些相关的规定？",
    "在什么情况下不适用？",
]
#: Mock 模式的确定性会话标题（F5）
_MOCK_TITLE = "知识库问答"


class LLMError(Exception):
    """LLM 调用失败，携带错误码供上层映射统一信封。"""

    def __init__(self, code: ErrCode, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

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
    def generate(self, prompt: str) -> str: ...

    def stream(self, prompt: str) -> Iterator[str]: ...

    # 最近一次调用的 token 用量。刻意**不改 generate/stream 签名**：
    # 那是 6 处调用方的公共契约，改返回值会让所有适配器一起返工；改为在实例上
    # 暴露只读属性，调用方用 getattr(llm, "last_usage", None) 兜底即可。
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
    ):
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

    def _call_stream(self, prompt: str) -> Iterator[str]:
        from dashscope import Generation

        from app.services.security.platform_quota import get_monitor

        last_error: Exception | None = None
        produced: list[str] = []
        self._last_usage = TokenUsage.estimate(prompt)
        for attempt in range(self._max_retries):
            try:
                get_monitor().record_call(self.model)
                # 批次 2：上游隔离仓。许可覆盖整段流式消费——并发控制的对象是
                # "在飞的上游调用"，不是单次 HTTP 往返。熔断打开时快速失败不占线程。
                with bulkhead("llm").acquire(timeout_s=self._timeout_s):
                    responses = Generation.call(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        temperature=self.temperature,
                        result_format="message",
                        stream=True,
                        incremental_output=True,
                        api_key=self._api_key,
                        timeout=self._timeout_s,
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
                # 平台配额此前只记调用次数（record_call 的 tokens 形参从未被使用），
                # 现在把真实 token 补记上去，成本才能按模型归集。
                try:
                    get_monitor().record_call(
                        self.model,
                        tokens=self._last_usage.total,
                        estimated=self._last_usage.estimated,
                    )
                except TypeError:
                    pass  # 兼容未升级的 monitor 签名
                return
            except LLMError:
                raise
            except CircuitOpenError as exc:
                # 熔断打开不重试：打开期间重试只会放大等待，降级交由上层决定
                raise LLMError(ErrCode.DEPENDENCY_UNAVAILABLE, f"上游熔断（{exc.upstream}），请稍后重试") from exc
            except Exception as exc:  # noqa: BLE001 - 网络类异常走重试
                last_error = exc
                if attempt == self._max_retries - 1:
                    break
                wait = 2**attempt
                logger.warning("LLM 调用失败，%ds 后重试（%d/%d）: %s", wait, attempt + 1, self._max_retries, exc)
                time.sleep(wait)
        raise LLMError(ErrCode.GENERATION_FAILED, f"生成失败: {last_error}")

    @staticmethod
    def _raise_for(status_code: int, message: str) -> None:
        from app.services.security.platform_quota import get_monitor

        text = str(message)
        if status_code == 429 or "Throttl" in text:
            get_monitor().record_platform_error("rate_limited")
            raise LLMError(ErrCode.RATE_LIMITED, "平台限流，请稍后重试")
        if "quota" in text.lower() or "Allocation" in text:
            get_monitor().record_platform_error("quota")
            raise LLMError(ErrCode.QUOTA_EXCEEDED, "平台配额不足，请联系管理员")
        if "timeout" in text.lower():
            get_monitor().record_platform_error("timeout")
            raise LLMError(ErrCode.GENERATION_TIMEOUT, "生成超时，请重试")
        get_monitor().record_platform_error("other")
        raise LLMError(ErrCode.GENERATION_FAILED, f"生成失败: {message}")

    def generate(self, prompt: str) -> str:
        return "".join(self._call_stream(prompt))

    def stream(self, prompt: str) -> Iterator[str]:
        return self._call_stream(prompt)

    # ---- F5：推荐追问 / 会话标题 ----

    def generate_followups(self, answer: str, n: int = 3) -> list[str]:
        return generate_followups(self, answer, n)

    def generate_title(self, question: str, answer: str) -> str:
        return generate_title(self, question, answer)


class MockLLM:
    """确定性假模型：从 prompt 中解析证据编号，生成带引用的可预期答案。"""

    _NO_INFO = "知识库无相关内容"

    def __init__(self):
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

    def generate(self, prompt: str) -> str:
        text = self._answer(prompt)
        self._last_usage = TokenUsage.estimate(prompt, text)
        return text

    # ---- F5：确定性追问与标题（供测试断言固定输出）----

    def generate_followups(self, answer: str, n: int = 3) -> list[str]:
        return _MOCK_FOLLOWUPS[:n]

    def generate_title(self, question: str, answer: str) -> str:
        return _MOCK_TITLE

    def stream(self, prompt: str) -> Iterator[str]:
        text = self._answer(prompt)
        self._last_usage = TokenUsage.estimate(prompt, text)
        # 按小片段吐出，模拟流式
        step = 12
        for i in range(0, len(text), step):
            yield text[i : i + step]

    @property
    def last_usage(self) -> TokenUsage:
        return self._last_usage


def build_llm(provider: str, api_key: str, model: str, temperature: float, max_retries: int, timeout_s: float) -> LLM:
    if provider == "dashscope":
        return QwenLLM(
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_retries=max_retries,
            timeout_s=timeout_s,
        )
    logger.info("使用 MockLLM（未配置千问 Key 或显式指定 mock）")
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("生成会话标题失败（已忽略）: %s", exc)
        return ""
    title = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    title = title.strip("\"'“”‘’ ")
    return title[:40]

