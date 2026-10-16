"""查询扩展器组（阶段 4 §3.2 / §3.3 / §3.4）。

- FusionExpander：N 个语义等价改写（经验值 N=3，边际递减）；
- HydeExpander：先让模型想象理想答案，用假设答案去检索（通用领域提升显著）；
- StepBackExpander：把具体问题抽象为原则性问题（"为什么/原理"类适用）。

HyDE 与 Fusion 不同时启用（互相稀释）。每个扩展器独立可注入、可测试、可替换；
单个失败只标记该条改写未使用，不阻塞其它扩展。
"""
import logging

from pydantic import BaseModel, Field

from app.services.llm import LLM
from app.services.query.jsonutil import JsonParseError, parse_json
from app.services.query.schemas import REWRITE_HYDE, REWRITE_REWRITE, REWRITE_STEPBACK, Rewrite

logger = logging.getLogger(__name__)

FUSION_PROMPT = """把下面的用户问题改写成 {n} 个语义等价但措辞不同的检索用问句（更规范、更书面、覆盖可能的同义术语）。
只输出 JSON 数组，不要输出其它内容。

用户问题：{question}

输出 JSON：["改写1", "改写2", "改写3"]"""

HYDE_PROMPT = """请针对下面的问题，直接写一段 100 字左右、仿佛是知识库文档原文的假设性回答（使用规范术语与陈述语气，不要说"可能/假设"）：

问题：{question}"""

STEPBACK_PROMPT = """把下面的具体问题抽象成一个更通用、更原则性的问题（去掉具体型号/对象名，保留核心概念），只输出抽象后的问题本身，不要解释：

具体问题：{question}"""


class _FusionPayload(BaseModel):
    rewrites: list[str] = Field(default_factory=list)


class FusionExpander:
    name = "fusion"

    def __init__(self, llm: LLM, n: int = 3):
        self._llm = llm
        self._n = n

    def expand(self, question: str) -> list[Rewrite]:
        raw = self._llm.generate(FUSION_PROMPT.format(question=question, n=self._n))
        data = parse_json(raw)
        texts = [t for t in data if isinstance(t, str) and t.strip()][: self._n] if isinstance(data, list) else []
        if not texts:
            raise JsonParseError("改写结果为空")
        return [Rewrite(type=REWRITE_REWRITE, text=t.strip(), expander=self.name) for t in texts]


class HydeExpander:
    name = "hyde"

    def __init__(self, llm: LLM):
        self._llm = llm

    def expand(self, question: str) -> list[Rewrite]:
        text = self._llm.generate(HYDE_PROMPT.format(question=question)).strip()
        if not text:
            raise JsonParseError("HyDE 输出为空")
        return [Rewrite(type=REWRITE_HYDE, text=text, expander=self.name)]


class StepBackExpander:
    name = "stepback"

    def __init__(self, llm: LLM):
        self._llm = llm

    def expand(self, question: str) -> list[Rewrite]:
        text = self._llm.generate(STEPBACK_PROMPT.format(question=question)).strip()
        if not text or text == question:
            raise JsonParseError("Step-Back 未产出有效抽象问题")
        return [Rewrite(type=REWRITE_STEPBACK, text=text, expander=self.name)]
