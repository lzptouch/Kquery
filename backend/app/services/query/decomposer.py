"""多跳问题拆解与数据源路由。

- 只在特定意图触发（compare / summarize）：默认不拆、按需拆，
  强制拆解会让简单问题消耗 2~3 倍 LLM 调用；
- 每个子问题自带 expected_source（LLM 推荐），路由层采纳并去重；
- 特殊意图强制加源：calc → +sql；提及"最新/今天" → +web；
- 兜底：拆解为空或全部未知 → 至少包含 vector（不能无源可查）；
- 拆解失败 → 降级为单跳（48xx 语义，不阻塞主链路）。
"""

from app.services.llm import LLM
from app.services.query.jsonutil import JsonParseError, parse_json
from app.services.query.schemas import SubQuestion
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

DECOMPOSE_PROMPT = """把下面的复合问题拆解为 2~4 个可独立检索的简单子问题。
每个子问题标注期望数据源：vector（文档知识库）/ sql（数值统计）/ web（时效信息）/ graph（实体关系）。
只输出 JSON 数组，不要输出其它内容。

复合问题：{question}

输出 JSON：[{{"text": "子问题1", "expected_source": "vector"}}, ...]"""

FRESHNESS_MARKERS = ("最新", "今天", "现在", "近期", "最近", "实时")

# 本阶段已接入的数据源（其余路由决策如实记录但跳过检索）
ACTIVE_SOURCES = {"vector"}


class Decomposer:
    name = "decomposer"

    def __init__(self, llm: LLM, max_sub_questions: int = 4) -> None:
        self._llm = llm
        self._max = max_sub_questions

    def decompose(self, question: str) -> list[SubQuestion]:
        raw = self._llm.generate(DECOMPOSE_PROMPT.format(question=question))
        data = parse_json(raw)
        if not isinstance(data, list) or not data:
            raise JsonParseError("拆解结果为空")
        sub_questions: list[SubQuestion] = []
        for item in data[: self._max]:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            source = item.get("expected_source", "vector")
            if source not in {"vector", "sql", "web", "graph"}:
                source = "vector"
            sub_questions.append(
                SubQuestion(text=item["text"].strip(), expected_source=source)
            )
        if not sub_questions:
            raise JsonParseError("拆解结果无有效子问题")
        return sub_questions


class SourceRouter:
    """数据源路由：采纳子问题推荐源 + 强制规则 + 兜底 vector。"""

    def route(self, question: str, intent: str, sub_questions: list[SubQuestion]) -> list[str]:
        sources: list[str] = []
        for sq in sub_questions:
            if sq.expected_source not in sources:
                sources.append(sq.expected_source)
        if intent == "calc" and "sql" not in sources:
            sources.append("sql")
        if any(marker in question for marker in FRESHNESS_MARKERS) and "web" not in sources:
            sources.append("web")
        if not sources:
            sources = ["vector"]
        return sources


def mark_inactive_sources_skipped(sub_questions: list[SubQuestion]) -> list[SubQuestion]:
    """未接入数据源的子问题如实标记跳过（路由决策保留，检索执行跳过）。"""
    for sq in sub_questions:
        if sq.expected_source not in ACTIVE_SOURCES and not sq.skipped:
            sq.skipped = True
            sq.skip_reason = f"数据源 {sq.expected_source} 尚未接入，已按 vector 兜底检索"
            sq.expected_source = "vector"
    return sub_questions
