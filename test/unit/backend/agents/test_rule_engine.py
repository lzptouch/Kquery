import pytest

from app.agents.rule_engine import BizRuleEngine, Rule, RuleAction, RuleResult


@pytest.mark.unit
class TestBizRuleEngine:
    def test_add_rule_and_evaluate_match(self) -> None:
        engine = BizRuleEngine()
        engine.add_rule(Rule(
            name="greeting",
            condition=lambda ctx: "你好" in ctx.get("question", ""),
            action=RuleAction.RECOMMEND,
            priority=100,
            description="问候规则",
        ))
        results = engine.evaluate("greeting", {"question": "你好"})
        assert isinstance(results, list)
        assert any(r.matched and r.rule_name == "greeting" for r in results)

    def test_priority_order(self) -> None:
        engine = BizRuleEngine()
        engine.add_rule(Rule(
            name="low",
            condition=lambda ctx: True,
            action=RuleAction.ALLOW,
            priority=10,
        ))
        engine.add_rule(Rule(
            name="high",
            condition=lambda ctx: True,
            action=RuleAction.DENY,
            priority=100,
        ))
        results = engine.evaluate("priority", {"question": "x"})
        # 高优先级规则应排在前面
        assert results[0].rule_name == "high"

    def test_no_match(self) -> None:
        engine = BizRuleEngine()
        engine.add_rule(Rule(
            name="math",
            condition=lambda ctx: "计算" in ctx.get("question", ""),
            action=RuleAction.RECOMMEND,
            priority=50,
        ))
        results = engine.evaluate("math", {"question": "闲聊"})
        assert not any(r.matched for r in results)

    def test_rule_exception_isolation(self) -> None:
        engine = BizRuleEngine()
        engine.add_rule(Rule(
            name="broken",
            condition=lambda ctx: (_ for _ in ()).throw(ValueError("boom")),
            action=RuleAction.CUSTOM,
            priority=10,
        ))
        engine.add_rule(Rule(
            name="fallback",
            condition=lambda ctx: True,
            action=RuleAction.ALLOW,
            priority=5,
        ))
        results = engine.evaluate("exception", {"question": "x"})
        # 异常规则的评估应被隔离，fallback 仍可匹配
        assert any(r.rule_name == "fallback" and r.matched for r in results)
