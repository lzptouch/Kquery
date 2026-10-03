"""意图识别（阶段 4 §3.1）。

- 生产：千问 qwen-turbo——每次提问都触发的轻量判定，输入短、任务简单、
  对时延敏感，压到最低档是预处理环节最重要的成本控制；
- 提示词 + 自定义 JSON 解析（不用 function calling）——换平台换档位解析逻辑不变；
- 失败兜底：默认"需要检索"（46xx 静默降级，不阻塞主链路）。
"""
import logging

from pydantic import BaseModel

from app.services.llm import LLM, LLMError
from app.services.query.jsonutil import JsonParseError, parse_json_model
from app.services.query.schemas import IntentResult

logger = logging.getLogger(__name__)

INTENT_PROMPT = """你是意图分类器。判断用户问题的意图类型，只输出 JSON，不要输出其它内容。

意图类型：
- chitchat：闲聊问候，与知识库无关（如"你好""你是谁""讲个笑话"）
- fact_qa：事实型问答（如"电池电压是多少"）
- compare：多对象对比（如"A 和 B 哪个好"）
- summarize：文档/内容总结（如"这份文档讲了什么"）
- calc：涉及计算统计（如"平均值是多少"）
- code：代码相关
- routing：操作类请求

用户问题：{question}

输出 JSON：{{"intent": "类型", "needs_retrieval": true/false, "confidence": 0.0~1.0, "reasoning": "一句话判定依据"}}
needs_retrieval 规则：chitchat 为 false，其余为 true。"""


class _IntentPayload(BaseModel):
    intent: str = "fact_qa"
    needs_retrieval: bool = True
    confidence: float = 0.0
    reasoning: str = ""


class IntentClassifier:
    def __init__(self, llm: LLM, max_retries: int = 1):
        self._llm = llm
        self._max_retries = max_retries

    def classify(self, question: str) -> IntentResult:
        """失败时抛 JsonParseError/LLMError，由编排器降级为默认需要检索。"""
        prompt = INTENT_PROMPT.format(question=question)
        last_error: Exception | None = None
        for _ in range(self._max_retries + 1):
            try:
                raw = self._llm.generate(prompt)
                payload = parse_json_model(raw, _IntentPayload)
                # chitchat 强制短路（不信任模型自报的 needs_retrieval）
                needs = payload.needs_retrieval and payload.intent != "chitchat"
                return IntentResult(
                    intent=payload.intent,
                    needs_retrieval=needs,
                    confidence=max(0.0, min(1.0, payload.confidence)),
                    reasoning=payload.reasoning,
                )
            except (JsonParseError, LLMError) as exc:
                last_error = exc
                logger.warning("意图识别重试: %s", exc)
        raise JsonParseError(f"意图识别失败: {last_error}")
