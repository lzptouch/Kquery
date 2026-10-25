"""F5 推荐追问与会话命名单测。"""
import json


from app.services.llm import (
    MockLLM,
    _parse_followups,
    generate_followups,
    generate_title,
)


class _BoomLLM:
    def generate(self, prompt: str) -> str:
        raise RuntimeError("LLM 不可用")

    last_usage = None


class _JsonLLM:
    def __init__(self, payload: str):
        self.payload = payload
        self.last_usage = None

    def generate(self, prompt: str) -> str:
        return self.payload


class TestFollowups:
    def test_returns_three(self):
        llm = MockLLM()
        items = llm.generate_followups("答案内容", 3)
        assert len(items) == 3
        assert all(isinstance(i, str) and i.strip() for i in items)

    def test_module_helper_uses_method(self):
        items = generate_followups(MockLLM(), "答案内容", 3)
        assert len(items) == 3

    def test_llm_failure_returns_empty(self):
        """LLM 挂掉 → 空列表，**不外溢**（追问失败不能影响主答案）。"""
        assert generate_followups(_BoomLLM(), "答案内容", 3) == []

    def test_malformed_json_tolerated(self):
        assert generate_followups(_JsonLLM("{不是 json"), "答案", 3) == []

    def test_json_array_parsed(self):
        raw = json.dumps(["问题一？", "问题二？", "问题三？"], ensure_ascii=False)
        assert generate_followups(_JsonLLM(raw), "答案", 3) == ["问题一？", "问题二？", "问题三？"]

    def test_plain_lines_parsed(self):
        raw = "第一个问题？\n第二个问题？\n第三个问题？"
        items = generate_followups(_JsonLLM(raw), "答案", 3)
        assert items == ["第一个问题？", "第二个问题？", "第三个问题？"]

    def test_empty_answer_returns_empty(self):
        assert generate_followups(MockLLM(), "   ", 3) == []

    def test_parse_respects_limit(self):
        raw = "\n".join(f"问题{i}？" for i in range(10))
        assert len(_parse_followups(raw, 3)) == 3


class TestTitle:
    def test_generates_title(self):
        title = generate_title(MockLLM(), "问题", "答案")
        assert title and len(title) <= 40

    def test_failure_returns_empty(self):
        assert generate_title(_BoomLLM(), "问题", "答案") == ""

    def test_strips_quotes(self):
        raw = _JsonLLM('"带引号的标题"')
        assert generate_title(raw, "问题", "答案") == "带引号的标题"
