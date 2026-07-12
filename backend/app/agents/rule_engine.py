"""业务规则引擎。

定义和执行销售策略规则，支持条件评估和规则链执行。
归属于L5核心决策层。

使用示例:
    from app.agents.rule_engine import biz_rule_engine

    result = biz_rule_engine.evaluate("customer_priority", {
        "industry": "tech",
        "annual_revenue": 5000000,
        "existing_relationship": True,
    })
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.logger import get_logger

logger = get_logger(__name__)


class RuleAction(str, Enum):
    """规则动作枚举。"""
    ALLOW = "allow"
    DENY = "deny"
    ESCALATE = "escalate"        # 升级处理
    RECOMMEND = "recommend"      # 推荐操作
    NOTIFY = "notify"            # 通知相关人员
    CUSTOM = "custom"            # 自定义动作


@dataclass
class Rule:
    """业务规则定义。

    Attributes:
        name: 规则名称。
        condition: 条件评估函数。
        action: 规则动作。
        priority: 优先级（数值越大优先级越高）。
        description: 规则描述。
    """
    name: str
    condition: Callable[[Dict[str, Any]], bool]
    action: RuleAction
    priority: int = 0
    description: str = ""


@dataclass
class RuleResult:
    """规则评估结果。

    Attributes:
        rule_name: 触发规则名称。
        action: 建议动作。
        matched: 是否匹配。
        score: 匹配分数 (0-100)。
        details: 评估详情。
    """
    rule_name: str
    action: RuleAction
    matched: bool
    score: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)


class BizRuleEngine:
    """业务规则引擎。

    管理规则集合，支持条件评估、优先级排序和规则链执行。
    """

    def __init__(self) -> None:
        self._rules: List[Rule] = []
        self._initialized = False

    def initialize_default_rules(self) -> None:
        """初始化默认业务规则。"""
        if self._initialized:
            return

        self.add_rule(Rule(
            name="high_value_customer",
            condition=lambda ctx: ctx.get("annual_revenue", 0) > 1000000,
            action=RuleAction.RECOMMEND,
            priority=10,
            description="高价值客户优先跟进",
        ))
        self.add_rule(Rule(
            name="competitive_scenario",
            condition=lambda ctx: ctx.get("has_competitor", False),
            action=RuleAction.ESCALATE,
            priority=8,
            description="有竞品参与时升级处理",
        ))
        self.add_rule(Rule(
            name="renewal_reminder",
            condition=lambda ctx: ctx.get("contract_expiring_soon", False),
            action=RuleAction.NOTIFY,
            priority=7,
            description="合同即将到期提醒",
        ))
        self.add_rule(Rule(
            name="cross_sell_opportunity",
            condition=lambda ctx: ctx.get("existing_products", []) and
            len(ctx.get("existing_products", [])) < 3,
            action=RuleAction.RECOMMEND,
            priority=5,
            description="交叉销售机会识别",
        ))

        self._initialized = True
        logger.info(f"默认规则引擎已初始化: {len(self._rules)} 条规则")

    def add_rule(self, rule: Rule) -> None:
        """添加规则。

        Args:
            rule: 规则定义。
        """
        self._rules.append(rule)
        self._rules.sort(key=lambda r: r.priority, reverse=True)

    def evaluate(
        self,
        context_name: str,
        context: Dict[str, Any],
    ) -> List[RuleResult]:
        """评估指定上下文下的所有规则。

        Args:
            context_name: 上下文名称（用于日志）。
            context: 上下文数据字典。

        Returns:
            匹配的规则结果列表（按优先级降序）。
        """
        if not self._initialized:
            self.initialize_default_rules()

        results: List[RuleResult] = []

        for rule in self._rules:
            try:
                matched = rule.condition(context)
                if matched:
                    score = self._calculate_score(rule, context)
                    results.append(RuleResult(
                        rule_name=rule.name,
                        action=rule.action,
                        matched=True,
                        score=score,
                        details={"context": context_name, "priority": rule.priority},
                    ))
                    logger.debug(
                        f"规则匹配: {rule.name} (score={score}) "
                        f"context={context_name}"
                    )
            except Exception as e:
                logger.warning(f"规则评估失败: {rule.name}, error={e}")

        return results

    def evaluate_first(
        self,
        context_name: str,
        context: Dict[str, Any],
    ) -> Optional[RuleResult]:
        """评估并返回第一个匹配的规则。

        Args:
            context_name: 上下文名称。
            context: 上下文数据。

        Returns:
            第一个匹配的规则结果，无匹配返回None。
        """
        results = self.evaluate(context_name, context)
        return results[0] if results else None

    def get_rules(self) -> List[Rule]:
        """获取所有规则。

        Returns:
            规则列表。
        """
        return list(self._rules)

    def clear_rules(self) -> None:
        """清除所有规则。"""
        self._rules.clear()
        self._initialized = False

    def _calculate_score(self, rule: Rule, context: Dict[str, Any]) -> float:
        """计算规则的匹配分数。

        Args:
            rule: 规则定义。
            context: 上下文。

        Returns:
            分数 (0-100)。
        """
        base_score = min(rule.priority * 10, 70)
        confidence = context.get("confidence", 0.5)
        return min(base_score + confidence * 30, 100)


# 全局单例
biz_rule_engine = BizRuleEngine()
