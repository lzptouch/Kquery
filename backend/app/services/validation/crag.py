"""CRAG 三分支路由。

| 分支 | 平均分范围 | 行动 |
| --- | --- | --- |
| correct | ≥ correct_th | 直接生成 |
| ambiguous | incorrect_th < avg < correct_th | 精炼查询 → 重检索（最多 max_iterations 轮） |
| incorrect | ≤ incorrect_th | 兜底来源 / 拒答 |

- 分数口径与 重排一致（gte-rerank-v2 相关度分），不混用评分器；
- 循环上限必需：第二次仍不达标意味着问题在知识库本身而非检索措辞；
- 每一轮都在改变提问角度（精炼），而不是重复提问。
"""
import logging

from pydantic import BaseModel

from app.services.llm import LLM

logger = logging.getLogger(__name__)

BRANCH_CORRECT = "correct"
BRANCH_AMBIGUOUS = "ambiguous"
BRANCH_INCORRECT = "incorrect"

REFINE_PROMPT = """基于原始问题与当前检索到的片段，生成一个更可能命中相关文档的精炼检索查询。
要求：改变提问角度（而非重复原问），使用更规范的术语，突出缺失的信息点。
只输出精炼后的查询本身，不要解释。

原始问题：{question}
当前命中片段摘要：{evidence_hints}"""


class CragRound(BaseModel):
    round: int
    branch: str
    avg_score: float
    evidence_count: int
    refined_query: str = ""


class CragRouter:
    """CRAG 三分支路由。

    **阈值必须显式传入**（此前有 `0.7/0.3` 的构造默认值，与
    `config.crag_correct_th/crag_incorrect_th` 的标定值 `0.14/0.08` 构成
    两个来源）。这两个默认值在生产里从未生效（`main.py` 总是显式传参），
    但它们是纯粹的陷阱——任何不传参的构造都会得到与标定口径不一致的阈值，
    且**不会有任何报错**。宁可构造失败，也不要静默用错阈值。
    """

    def __init__(
        self,
        correct_th: float,
        incorrect_th: float,
        max_iterations: int = 2,
    ):
        if correct_th <= incorrect_th:
            raise ValueError(
                f"CRAG 阈值必须 correct_th > incorrect_th（得 {correct_th} / {incorrect_th}）"
            )
        self._correct_th = correct_th
        self._incorrect_th = incorrect_th
        self.max_iterations = max_iterations

    def decide(self, avg_score: float) -> str:
        if avg_score >= self._correct_th:
            return BRANCH_CORRECT
        if avg_score <= self._incorrect_th:
            return BRANCH_INCORRECT
        return BRANCH_AMBIGUOUS

    def refine_query(self, llm: LLM, question: str, evidence_hints: str) -> str:
        """精炼产物不是给用户的，而是给下一轮检索的查询线索。"""
        refined = llm.generate(
            REFINE_PROMPT.format(question=question, evidence_hints=evidence_hints or "（无）")
        ).strip()
        return refined or question
