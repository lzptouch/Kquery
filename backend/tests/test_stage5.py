"""阶段 5 功能测试：CRAG 三分支 / 幻觉自检 / 受限重生成 / 显式拒答 / 质量指标。

- correct：正常问题 answered + confidence + 轮次记录 + 未采纳证据随返；
- incorrect：零相关查询 → refused(no_content)，HTTP 200 一等公民；
- ambiguous：中间分 → 精炼重检索循环 → 耗尽后 refused(insufficient_evidence)；
- 幻觉自检：有无据声明 → 重生成一次 → 二次仍有 → 置信度折减；自检降级 → 未知；
- 反馈工单与质量指标端点。
"""
import pytest

from app.services.validation.selfcheck import SelfCheckResult, compute_confidence
from tests.conftest import SAMPLE_MD, upload_sample

LONG_MD = """# 电池产品白皮书

## 第一章 电池规格

XB-2024 电池的标称电压为 24V，容量 5000mAh，采用磷酸铁锂电芯，循环寿命超过 2000 次。
该电池的工作温度范围为零下 20 度到 60 度，支持低温自加热功能，适合北方地区冬季使用。
电池内置智能管理芯片，可以实时监测电压、电流与温度，并在异常时自动断电保护。

## 第二章 充电规范

XB-2024 电池推荐使用 CD-100 充电器进行充电，充电电流不得超过 10A。
充电环境温度应保持在 0 度到 45 度之间，低于 0 度时充电器会自动降低电流以保护电芯。
完整充电时间约为 2 小时，电量从 20% 充至 80% 仅需 40 分钟。

## 第三章 保养与存放

长期存放电池时，应将电量保持在 40% 到 60% 之间，并放置于阴凉干燥处。
每三个月应进行一次充放电循环，以防止电芯活性下降。
"""


class TestCragCorrect:
    def test_answered_with_confidence_and_rounds(self, client):
        client.post("/knowledge/upload", files={"file": ("白皮书.md", LONG_MD.encode(), "text/markdown")})
        resp = client.post("/ask", json={"question": "XB-2024 电池的标称电压是多少？"})
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["verdict"] == "answered"
        assert data["confidence"] == 1.0  # mock 自检全部有据
        assert data["crag"]["branch"] == "correct"
        assert data["crag"]["rounds"][0]["avg_score"] > 0
        assert "24V" in str(data["evidence"][0]["content"]) or "24V" in data["answer"]

    def test_rejected_evidence_returned_with_reason(self, client):
        """未采纳证据也必须返回（used=False + 原因可归类）。"""
        client.post("/knowledge/upload", files={"file": ("白皮书.md", LONG_MD.encode(), "text/markdown")})
        data = client.post("/ask", json={"question": "电池的标称电压是多少？"}).json()["data"]
        rejected = [e for e in data["evidence"] if not e["used"]]
        used = [e for e in data["evidence"] if e["used"]]
        assert used and all(e["used"] for e in used)
        if rejected:  # 候选充足时应存在未采纳证据
            assert all(e["rejected_reason"] for e in rejected)


class TestCragRefusal:
    def test_incorrect_branch_refuses_no_content(self, client):
        client.post("/knowledge/upload", files={"file": ("白皮书.md", LONG_MD.encode(), "text/markdown")})
        resp = client.post("/ask", json={"question": "量子引力波调制器的校准周期是多少？"})
        assert resp.status_code == 200  # 拒答是一等公民，不走错误码
        data = resp.json()["data"]
        assert data["verdict"] == "refused"
        assert data["refusal_reason"] == "no_content"
        assert data["confidence"] == 0.0
        assert data["evidence"] == []
        assert data["answer"]  # 拒答语存在

    def test_ambiguous_exhausts_iterations_then_refuses(self, client):
        """中间区间 → 精炼重检索循环 → 上限耗尽 → refused(insufficient_evidence)。"""
        client.post("/knowledge/upload", files={"file": ("白皮书.md", LONG_MD.encode(), "text/markdown")})

        class MidReranker:
            model = "mid"

            def rerank(self, query, candidates, top_n):
                for c in candidates:
                    c.rerank_score = 0.2  # 落在 (0.1, 0.3) 中间区间
                return candidates[:top_n]

        client.app.state.hybrid._reranker = MidReranker()
        resp = client.post("/ask", json={"question": "电池的电压是多少？"})
        data = resp.json()["data"]
        assert data["verdict"] == "refused"
        assert data["refusal_reason"] == "insufficient_evidence"
        # 循环上限生效：1 + max_iterations(2) = 3 轮，且记录了精炼查询
        assert len(data["crag"]["rounds"]) == 3
        assert data["crag"]["rounds"][0]["refined_query"]

    def test_sse_refused_event(self, client):
        client.post("/knowledge/upload", files={"file": ("白皮书.md", LONG_MD.encode(), "text/markdown")})
        with client.stream("POST", "/ask/stream", json={"question": "量子引力波调制器的校准周期是多少？"}) as resp:
            text = "".join(resp.iter_text())
        assert "event: refused" in text
        assert '"reason": "no_content"' in text
        assert "event: done" in text


class TestSelfCheck:
    def test_hallucination_triggers_single_regeneration(self, client):
        upload_sample(client)

        class FakeChecker:
            def __init__(self):
                self.calls = 0

            def check(self, answer, evidence_block):
                self.calls += 1
                if self.calls == 1:
                    return SelfCheckResult(
                        is_hallucination=True, unsupported_claims=["电压为 5V"], reasoning="证据中未提及 5V"
                    )
                return SelfCheckResult(is_hallucination=False, unsupported_claims=[], reasoning="全部有据")

            def regenerate(self, question, evidence_block, claims):
                return "电池标称电压为 24V [#1]。\n引用片段编号：[#1]"

        checker = FakeChecker()
        client.app.state.pipeline._self_checker = checker
        data = client.post("/ask", json={"question": "支持哪些文档格式？"}).json()["data"]
        assert data["verdict"] == "answered"
        assert data["regenerated"] is True
        assert checker.calls == 2  # 初次 + 二次校验
        assert data["confidence"] == 1.0  # 二次校验全部有据
        assert "24V" not in data["answer"] or "5V" not in data["answer"]

    def test_persistent_claims_reduce_confidence(self, client):
        upload_sample(client)

        class StubbornChecker:
            def check(self, answer, evidence_block):
                return SelfCheckResult(
                    is_hallucination=True,
                    unsupported_claims=["声明甲", "声明乙", "声明丙"],
                    reasoning="仍有无据声明",
                )

            def regenerate(self, question, evidence_block, claims):
                return "修正后的答案 [#1]。"

        client.app.state.pipeline._self_checker = StubbornChecker()
        data = client.post("/ask", json={"question": "支持哪些文档格式？"}).json()["data"]
        assert data["regenerated"] is True
        assert data["unsupported_claims_count"] == 3
        assert data["confidence"] == pytest.approx(0.4)  # 1 - 0.2×3

    def test_confidence_formula(self):
        assert compute_confidence(0) == 1.0
        assert compute_confidence(1) == pytest.approx(0.8)
        assert compute_confidence(3) == pytest.approx(0.4)
        assert compute_confidence(5) == 0.0
        assert compute_confidence(9) == 0.0  # 下界截断

    def test_selfcheck_degraded_returns_answer_with_unknown_confidence(self, client):
        upload_sample(client)

        class BrokenChecker:
            def check(self, answer, evidence_block):
                raise RuntimeError("模拟自检接口不可用")

            def regenerate(self, question, evidence_block, claims):
                raise RuntimeError("不应被调用")

        client.app.state.pipeline._self_checker = BrokenChecker()
        resp = client.post("/ask", json={"question": "支持哪些文档格式？"})
        data = resp.json()["data"]
        # 4Axx 降级：不阻断答案返回，置信度标为未知
        assert resp.status_code == 200
        assert data["verdict"] == "answered"
        assert data["answer"]
        assert data["confidence"] is None


class TestFeedbackAndQuality:
    def test_feedback_lifecycle(self, client):
        resp = client.post("/feedback/refusal", json={
            "feedback_type": "should_answer",
            "refusal_reason": "no_content",
            "user_note": "这个问题文档里应该有",
            "trace_id": "abc123",
        })
        assert resp.status_code == 200
        ticket_id = resp.json()["data"]["ticket_id"]
        assert ticket_id.startswith("fb-")

        ticket = client.get(f"/feedback/{ticket_id}").json()["data"]
        assert ticket["status"] == "pending"
        assert ticket["trace_id"] == "abc123"

        assert client.get("/feedback/nonexist").status_code == 404

    def test_feedback_requires_trace_id_and_valid_type(self, client):
        resp = client.post("/feedback/refusal", json={"feedback_type": "bad", "trace_id": "x"})
        # 契约从 422 修正为 400：校验失败也是业务错误，走统一信封
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] is not None

    def test_quality_metrics_endpoint(self, client):
        client.post("/knowledge/upload", files={"file": ("白皮书.md", LONG_MD.encode(), "text/markdown")})
        client.post("/ask", json={"question": "电池的标称电压是多少？"})
        client.post("/ask", json={"question": "量子引力波调制器的校准周期是多少？"})
        quality = client.get("/admin/metrics/quality").json()["data"]
        assert quality["answered"] >= 1
        assert quality["refused"] >= 1
        assert 0 < quality["refusal_rate"] < 1
        assert quality["faithfulness"] is not None
        assert "correct" in quality["crag_branch_ratio"] or "incorrect" in quality["crag_branch_ratio"]

    def test_sse_crag_and_confidence_events(self, client):
        client.post("/knowledge/upload", files={"file": ("白皮书.md", LONG_MD.encode(), "text/markdown")})
        with client.stream("POST", "/ask/stream", json={"question": "电池的标称电压是多少？"}) as resp:
            text = "".join(resp.iter_text())
        assert "event: crag_branch" in text
        assert '"branch": "correct"' in text
        assert "event: confidence" in text
        assert '"score": 1.0' in text
        assert "event: token" in text
