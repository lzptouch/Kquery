"""决策评估器。

评估Agent决策的质量和效果，支持多维度评分。
归属于L5核心决策层。

使用示例:
    from app.agents.decision_evaluator import decision_evaluator

    score = await decision_evaluator.evaluate(
        action="recommend_product",
        outcome={"conversion": True, "response_time": 2.5},
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DecisionScore:
    """决策评分。

    Attributes:
        total_score: 综合评分 (0-100)。
        relevance_score: 相关性评分。
        accuracy_score: 准确性评分。
        timeliness_score: 及时性评分。
        details: 评分详情。
    """
    total_score: float = 0.0
    relevance_score: float = 0.0
    accuracy_score: float = 0.0
    timeliness_score: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


class DecisionEvaluator:
    """决策评估器。

    从多个维度评估Agent决策的质量。
    """

    def __init__(self) -> None:
        self._history: List[Dict[str, Any]] = []

    async def evaluate(
        self,
        action: str,
        context: Dict[str, Any],
        outcome: Optional[Dict[str, Any]] = None,
    ) -> DecisionScore:
        """评估决策质量。

        Args:
            action: 执行的动作标识。
            context: 决策时的上下文。
            outcome: 决策结果（可选，用于结果反馈）。

        Returns:
            决策评分。
        """
        score = DecisionScore()

        # 相关性评分
        score.relevance_score = self._evaluate_relevance(action, context)

        # 准确性评分
        score.accuracy_score = self._evaluate_accuracy(action, context)

        # 及时性评分
        score.timeliness_score = self._evaluate_timeliness(context)

        # 综合评分
        score.total_score = (
            score.relevance_score * 0.4 +
            score.accuracy_score * 0.35 +
            score.timeliness_score * 0.25
        )

        score.details = {
            "action": action,
            "context_keys": list(context.keys()),
            "has_outcome": outcome is not None,
        }

        # 记录评估历史
        self._history.append({
            "action": action,
            "score": score.total_score,
            "timestamp": __import__("time").time(),
        })

        return score

    def get_average_score(self) -> float:
        """获取历史平均评分。

        Returns:
            平均评分。
        """
        if not self._history:
            return 0.0
        return sum(h["score"] for h in self._history) / len(self._history)

    def get_history(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取评估历史。

        Args:
            limit: 返回条数。

        Returns:
            评估历史列表。
        """
        return self._history[-limit:]

    def _evaluate_relevance(self, action: str, context: Dict[str, Any]) -> float:
        """评估决策相关性。

        Args:
            action: 动作标识。
            context: 上下文。

        Returns:
            相关性评分 (0-100)。
        """
        query = context.get("query", "")
        if not query:
            return 50.0
        return 80.0

    def _evaluate_accuracy(self, action: str, context: Dict[str, Any]) -> float:
        """评估决策准确性。

        Args:
            action: 动作标识。
            context: 上下文。

        Returns:
            准确性评分 (0-100)。
        """
        confidence = context.get("confidence", 0.7)
        return min(confidence * 100, 100)

    def _evaluate_timeliness(self, context: Dict[str, Any]) -> float:
        """评估决策及时性。

        Args:
            context: 上下文。

        Returns:
            及时性评分 (0-100)。
        """
        response_time = context.get("response_time", 0)
        if response_time <= 0:
            return 70.0
        if response_time < 1.0:
            return 95.0
        if response_time < 3.0:
            return 80.0
        if response_time < 5.0:
            return 60.0
        return 40.0


# 全局单例
decision_evaluator = DecisionEvaluator()
