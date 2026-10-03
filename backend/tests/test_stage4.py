"""阶段 4 功能测试：Query 预处理（意图/改写/拆解/路由/降级/分析记录）。

- JSON 容错：围栏 / 杂文本 / 结构校验；
- 意图识别：闲聊短路（不展示证据区）/ 事实问答走检索；
- 改写：Fusion 多条 + used 标记 + 预算截断标记；
- 拆解：compare 意图产生子问题；证据带 origin 双向定位；
- 降级：意图失败默认需要检索、拆解失败按单跳，主链路不中断且如实标注；
- 分析端点：历史列表 / 详情 / 模式统计。
"""
from app.services.query.jsonutil import JsonParseError, parse_json, parse_json_model
from app.services.query.orchestrator import QueryPreprocessor
from tests.conftest import SAMPLE_MD, upload_sample
import pytest
from pydantic import BaseModel


class TestJsonUtil:
    def test_strips_markdown_fence(self):
        assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_strips_surrounding_prose(self):
        assert parse_json('分类结果：{"a": 1} 以上。') == {"a": 1}

    def test_array_extraction(self):
        assert parse_json('输出：["x", "y"]') == ["x", "y"]

    def test_model_validation(self):
        class M(BaseModel):
            a: int

        assert parse_json_model('{"a": 2}', M).a == 2
        with pytest.raises(JsonParseError):
            parse_json_model('{"b": 2}', M)

    def test_garbage_raises(self):
        with pytest.raises(JsonParseError):
            parse_json("完全不是 JSON 的回答")


class TestIntentAndShortCircuit:
    def test_chitchat_short_circuits(self, client):
        upload_sample(client)
        resp = client.post("/ask", json={"question": "你好"})
        data = resp.json()["data"]
        analysis = data["analysis"]
        assert analysis["intent"] == "chitchat"
        assert analysis["needs_retrieval"] is False
        # 闲聊短路：不展示证据区
        assert data["evidence"] == []
        assert data["answer"]  # 仍有直接回答

    def test_fact_qa_runs_full_pipeline(self, client):
        upload_sample(client)
        resp = client.post("/ask", json={"question": "支持哪些文档格式？"})
        data = resp.json()["data"]
        analysis = data["analysis"]
        assert analysis["intent"] == "fact_qa"
        assert analysis["needs_retrieval"] is True
        assert data["evidence"]
        # 改写集合：原始 + Fusion 多条（expander 标识与 used 标记齐全）
        types = [r["type"] for r in analysis["rewrites"]]
        assert "original" in types and "rewrite" in types
        for rw in analysis["rewrites"]:
            assert "used" in rw and "expander" in rw
        assert analysis["sources"] == ["vector"]
        # 证据带来源标注（双向定位支点）
        assert any(e.get("origin") for e in data["evidence"])

    def test_compare_intent_decomposes(self, client):
        upload_sample(client)
        resp = client.post("/ask", json={"question": "免费版和团队版对比哪个好？"})
        data = resp.json()["data"]
        analysis = data["analysis"]
        assert analysis["intent"] == "compare"
        assert len(analysis["sub_questions"]) >= 2
        for sq in analysis["sub_questions"]:
            assert sq["expected_source"]  # 期望数据源必填

    def test_stepback_expander_produces_abstract_query(self, client):
        upload_sample(client)
        data = client.post("/ask", json={"question": "XB-2024 电池能用多久？"}).json()["data"]
        types = [r["type"] for r in data["analysis"]["rewrites"]]
        assert "stepback" in types


class TestDegradation:
    def test_intent_failure_degrades_to_retrieval(self, client, monkeypatch):
        upload_sample(client)
        preprocessor = client.app.state.pipeline._preprocessor

        def boom(question):
            raise RuntimeError("模拟意图模型输出不可解析")

        monkeypatch.setattr(preprocessor._intent, "classify", boom)
        resp = client.post("/ask", json={"question": "支持哪些文档格式？"})
        data = resp.json()["data"]
        # 46xx 静默降级：主链路不中断，照常检索回答，并如实标注
        assert resp.status_code == 200
        assert data["evidence"]
        assert "intent" in data["analysis"]["degraded"]

    def test_decompose_failure_falls_back_single_hop(self, client, monkeypatch):
        upload_sample(client)
        preprocessor = client.app.state.pipeline._preprocessor

        def boom(question):
            raise RuntimeError("模拟拆解失败")

        monkeypatch.setattr(preprocessor._decomposer, "decompose", boom)
        resp = client.post("/ask", json={"question": "免费版和团队版对比哪个好？"})
        data = resp.json()["data"]
        assert resp.status_code == 200
        assert "decompose" in data["analysis"]["degraded"]


class TestBudgetTruncation:
    def test_overflow_rewrites_marked_unused(self):
        from app.services.llm import MockLLM
        from app.services.query.decomposer import Decomposer, SourceRouter
        from app.services.query.expanders import FusionExpander
        from app.services.query.intent import IntentClassifier

        llm = MockLLM()
        pre = QueryPreprocessor(
            IntentClassifier(llm),
            [FusionExpander(llm, n=5)],
            Decomposer(llm),
            SourceRouter(),
            query_budget=2,
        )
        analysis, items = pre.process("电池充电规范是什么？", "t")
        used = [r for r in analysis.rewrites if r.used]
        unused = [r for r in analysis.rewrites if not r.used]
        assert len(items) == 2
        assert unused and all(r.skip_reason for r in unused)
        assert used[0].type == "original"


class TestAnalysisEndpoints:
    def test_history_detail_and_patterns(self, client):
        upload_sample(client)
        client.post("/ask", json={"question": "你好"})
        client.post("/ask", json={"question": "支持哪些文档格式？"})
        client.post("/ask", json={"question": "免费版和团队版对比哪个好？"})

        # 历史列表（最新在前）
        rows = client.get("/knowledge/query-analysis").json()["data"]
        assert len(rows) >= 3
        assert rows[0]["intent"] == "compare"

        # 过滤：闲聊（短路）
        chitchat = client.get("/knowledge/query-analysis?needs_retrieval=false").json()["data"]
        assert len(chitchat) == 1 and chitchat[0]["intent"] == "chitchat"

        # 详情：全字段
        detail = client.get(f"/knowledge/query-analysis/{rows[0]['analysis_id']}").json()["data"]
        assert detail["rewrites"] and detail["sub_questions"] and detail["sources"]
        assert detail["strategy_version"]

        # 模式统计：意图分布
        patterns = client.get("/knowledge/query-patterns").json()["data"]
        intents = {p["intent"] for p in patterns}
        assert "chitchat" in intents and "fact_qa" in intents
        assert all(0 < p["ratio"] <= 1 for p in patterns)

        # 不存在 → 404
        resp = client.get("/knowledge/query-analysis/nonexist")
        assert resp.status_code == 404

    def test_sse_query_analysis_event(self, client):
        upload_sample(client)
        with client.stream("POST", "/ask/stream", json={"question": "支持哪些文档格式？"}) as resp:
            text = "".join(resp.iter_text())
        assert "event: query_analysis" in text
        assert '"intent": "fact_qa"' in text

    def test_preprocess_can_be_disabled(self, client):
        preprocessor = client.app.state.pipeline._preprocessor
        preprocessor._enabled = False
        data = client.post("/ask", json={"question": "你好"}).json()["data"]
        # 关闭预处理后闲聊也走检索（回到阶段 3 行为），分析标记 disabled
        assert "disabled" in data["analysis"]["degraded"]
