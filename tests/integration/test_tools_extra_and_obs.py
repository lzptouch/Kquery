"""工具集扩展与可观测（/134/135）测试。"""
import json
import logging
from datetime import date


class TestDateTimeCalcTool:
    """日期推理：LLM 直接算「N 个工作日后」错误率很高，必须有确定性工具。"""

    def _tool(self, today=(2026, 10, 16)):
        from app.services.agent.tools_extra import DateTimeCalcTool

        return DateTimeCalcTool(today=date(*today))

    def test_relative_days(self):
        r = self._tool().run("3天后是什么日期", "t1")
        assert not r.error and "2026-10-19" in r.items[0]["content"]

    def test_relative_days_back(self):
        r = self._tool().run("5天前", "t1")
        assert not r.error and "2026-10-11" in r.items[0]["content"]

    def test_chinese_numeral(self):
        r = self._tool().run("两周后", "t1")
        assert not r.error and "2026-10-30" in r.items[0]["content"]

    def test_workdays_skip_weekend(self):
        """2026-10-16 是周五；3 个工作日后 → 周三 10-21（跳过周末）。"""
        r = self._tool().run("3个工作日后", "t1")
        assert not r.error
        assert "2026-10-21" in r.items[0]["content"], r.items[0]["content"]
        assert "周末" in r.items[0]["content"]

    def test_month_end_rollover(self):
        """1/31 + 1 月 → 2/28（不是抛异常，也不是 3/3）。"""
        from app.services.agent.tools_extra import DateTimeCalcTool

        t = DateTimeCalcTool(today=date(2026, 1, 31))
        r = t.run("1个月后", "t1")
        assert not r.error and "2026-02-28" in r.items[0]["content"]

    def test_same_period_last_year(self):
        r = self._tool().run("去年同期", "t1")
        assert not r.error and "2025-10-16" in r.items[0]["content"]

    def test_date_diff(self):
        r = self._tool().run("2026-01-01 和 2026-03-01 相差多少天", "t1")
        assert not r.error and "59" in r.items[0]["content"]

    def test_unrecognised_returns_error_not_crash(self):
        r = self._tool().run("随便说点什么", "t1")
        assert r.error, "无法识别时应返回错误而不是瞎猜一个日期"

    def test_danger_is_safe(self):
        assert self._tool().danger == "safe"



class TestListDocumentsTool:
    """list_documents：补「库里有什么」这一盲区。

    ⚠️ 替身必须返回**真实的 `DocumentRecord`**，不能用 dict。
    初版替身用了 dict，而真实的 `KnowledgeService.list_files()` 返回 pydantic 模型，
    于是 `doc.get("filename")` 在线上必然抛 AttributeError、工具恒定失败——
    单元测试因为替身形态不符而全部通过，直到浏览器 E2E 才暴露
    （Planner 反复选这个工具 → 迭代耗尽 → 本可回答的问题被判拒答）。
    """

    @staticmethod
    def _rec(filename, **kw):
        from app.domain.schemas import DocumentRecord

        base = dict(
            doc_id=filename, file_id="f1", filename=filename,
            tenant_id="t1", source_path=f"/tmp/{filename}", content_hash="h",
        )
        base.update(kw)
        return DocumentRecord(**base)

    class _FakeKnowledge:
        def __init__(self, docs):
            self._docs = docs

        def list_files(self):
            return self._docs

    def test_lists_real_document_records(self):
        """核心回归：真实返回类型（DocumentRecord 对象）必须可用。"""
        from app.services.agent.tools_extra import ListDocumentsTool

        k = self._FakeKnowledge([
            self._rec("年报2026.pdf", chunk_count=12, status="indexed", uploaded_at="2026-10-01"),
            self._rec("制度.md", chunk_count=3, status="indexed", uploaded_at="2026-09-01"),
        ])
        r = ListDocumentsTool(k).run("", "t1")
        assert not r.error, f"真实返回类型下不得报错：{r.error}"
        assert len(r.items) == 2
        assert "年报2026.pdf" in r.items[0]["content"]
        assert "12" in r.items[0]["content"], "应包含块数（判断文档规模的依据）"

    def test_accepts_dicts_too(self):
        """同时兼容 dict 形态（便于其它调用方与测试）。"""
        from app.services.agent.tools_extra import ListDocumentsTool

        k = self._FakeKnowledge([{"filename": "a.md"}, {"filename": "b.md"}])
        r = ListDocumentsTool(k).run("", "t1")
        assert not r.error and len(r.items) == 2

    def test_keyword_filter(self):
        from app.services.agent.tools_extra import ListDocumentsTool

        k = self._FakeKnowledge([self._rec("年报.pdf"), self._rec("制度.md")])
        r = ListDocumentsTool(k).run("年报", "t1")
        assert len(r.items) == 1

    def test_keyword_miss_falls_back_to_full_list(self):
        """关键词是主题而非文件名时（'关于通货膨胀的文档'）必须回退全量，
        否则模型看不到"库里有哪些文档"这个关键判断依据。"""
        from app.services.agent.tools_extra import ListDocumentsTool

        k = self._FakeKnowledge([self._rec("曼昆经济学.md"), self._rec("制度.md")])
        r = ListDocumentsTool(k).run("通货膨胀", "t1")
        assert len(r.items) == 2, "主题词不匹配文件名时应回退全量清单"

    def test_knowledge_failure_returns_error(self):
        from app.services.agent.tools_extra import ListDocumentsTool

        class Broken:
            def list_files(self):
                raise RuntimeError("boom")

        r = ListDocumentsTool(Broken()).run("", "t1")
        assert r.error

    def test_empty_library(self):
        from app.services.agent.tools_extra import ListDocumentsTool

        r = ListDocumentsTool(self._FakeKnowledge([])).run("", "t1")
        assert not r.error and r.items == []

    def test_danger_is_read_only(self):
        from app.services.agent.tools_extra import ListDocumentsTool

        assert ListDocumentsTool(self._FakeKnowledge([])).danger == "read_only"


class TestStructuredLogging:
    """JSON 日志 + 强制字段 + PII 脱敏。"""

    def test_redact_phone_and_id(self):
        from app.core.logging_setup import redact

        assert "13800138000" not in redact("用户手机 13800138000")
        assert "11010119900307777X" not in redact("身份证 11010119900307777X")

    def test_redact_credentials(self):
        from app.core.logging_setup import redact

        out = redact("Authorization: Bearer abcdef1234567890")
        assert "abcdef1234567890" not in out
        assert "***" in out

    def test_json_formatter_has_required_fields(self):
        from app.core.logging_setup import REQUIRED_FIELDS, JsonFormatter

        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(rec))
        for f in REQUIRED_FIELDS:
            assert f in payload, f"缺少强制字段 {f}"

    def test_json_formatter_redacts_message(self):
        from app.core.logging_setup import JsonFormatter

        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=1,
            msg="手机 13800138000", args=(), exc_info=None,
        )
        payload = json.loads(JsonFormatter().format(rec))
        assert "13800138000" not in payload["msg"]

    def test_context_filter_injects_trace_id(self):
        from app.core.logging_setup import ContextFilter

        rec = logging.LogRecord(
            name="t", level=logging.INFO, pathname="", lineno=1,
            msg="x", args=(), exc_info=None,
        )
        ContextFilter().filter(rec)
        assert hasattr(rec, "trace_id")

    def test_debug_sampling_drops_but_keeps_info(self):
        from app.core.logging_setup import SamplingFilter

        f = SamplingFilter(debug_rate=0.01)  # 每 100 条取 1
        debug_recs = [
            logging.LogRecord("t", logging.DEBUG, "", 0, "d", (), None) for _ in range(50)
        ]
        passed = sum(1 for r in debug_recs if f.filter(r))
        assert 0 < passed < 50, "DEBUG 应被采样（不是全放也不是全丢）"

        info = logging.LogRecord("t", logging.INFO, "", 0, "i", (), None)
        assert f.filter(info) is True, "INFO 及以上不得采样"

    def test_sampling_zero_rate_still_blocks_debug(self):
        from app.core.logging_setup import SamplingFilter

        f = SamplingFilter(debug_rate=0.0)
        rec = logging.LogRecord("t", logging.DEBUG, "", 0, "d", (), None)
        assert f.filter(rec) is False

    def test_setup_logging_is_idempotent(self):
        from app.core.logging_setup import setup_logging

        setup_logging(level="INFO", log_format="text")
        setup_logging(level="INFO", log_format="text")
        assert len(logging.getLogger().handlers) == 1, "重复调用不应叠加 handler"


class TestCostLedger:
    """成本账单按阶段/模型/租户归集。"""

    def _ledger(self):
        from app.services.agent.cost import CostLedger

        return CostLedger()

    def test_for_trace_groups_by_phase(self):
        led = self._ledger()
        led.record("t1", "ten", "think", "qwen-plus", 100, 0.0001, True)
        led.record("t1", "ten", "answer", "qwen-plus", 200, 0.0002, True)
        bill = led.for_trace("t1")
        assert bill["found"] is True
        assert bill["total_tokens"] == 300
        assert set(bill["by_phase"]) == {"think", "answer"}
        assert bill["priced"] is True

    def test_unpriced_model_marks_bill_untrustworthy(self):
        """价格表缺模型时金额记 0 并标 priced=False（显示未知，而不是免费）。"""
        led = self._ledger()
        led.record("t2", "ten", "answer", "unknown-model", 500, 0.0, False)
        bill = led.for_trace("t2")
        assert bill["priced"] is False, "未定价的账单不得标为可信"

    def test_missing_trace_returns_zero_not_error(self):
        bill = self._ledger().for_trace("nope")
        assert bill["found"] is False and bill["total_yuan"] == 0.0

    def test_aggregate_by_phase_and_tenant(self):
        led = self._ledger()
        led.record("t1", "a", "think", "m", 10, 0.1, True)
        led.record("t2", "a", "answer", "m", 20, 0.2, True)
        led.record("t3", "b", "think", "m", 30, 0.3, True)

        by_phase = led.aggregate(group_by="phase")
        assert by_phase["groups"]["think"]["calls"] == 2
        assert by_phase["groups"]["answer"]["calls"] == 1

        by_tenant = led.aggregate(group_by="tenant_id")
        assert by_tenant["groups"]["a"]["tokens"] == 30

        only_a = led.aggregate(tenant_id="a")
        assert only_a["total_tokens"] == 30

    def test_top_traces_ranks_by_cost(self):
        led = self._ledger()
        led.record("cheap", "a", "answer", "m", 10, 0.01, True)
        led.record("pricey", "a", "answer", "m", 1000, 9.99, True)
        top = led.top_traces(limit=1)
        assert top[0]["trace_id"] == "pricey"

    def test_zero_usage_not_recorded(self):
        led = self._ledger()
        led.record("t", "a", "answer", "m", 0, 0.0, True)
        assert led.for_trace("t")["found"] is False

    def test_bounded_buffer(self):
        from app.services.agent.cost import CostLedger

        led = CostLedger(max_records=5)
        for i in range(10):
            led.record(f"t{i}", "a", "answer", "m", 1, 0.01, True)
        assert led.aggregate()["records"] == 5, "环形缓冲应有上限（避免无界增长）"


class TestCostEndpoints:
    def test_ask_cost_endpoint(self, client):
        r = client.get(
            "/ask/trace-does-not-exist/cost",
            headers={"X-Tenant-Id": "default", "X-User-Id": "admin", "X-Roles": "admin"},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["total_yuan"] == 0.0 and data["found"] is False

    def test_admin_costs_no_longer_role_gated(self, client):
        """角色门禁已下线：管理面端点不再按角色拦截。"""
        assert client.get("/admin/costs").status_code == 200
        assert client.get(
            "/admin/costs", headers={"X-User-Id": "alice", "X-Roles": ""}
        ).status_code == 200

    def test_admin_costs_shape(self, client):
        data = client.get("/admin/costs").json()["data"]
        assert {"group_by", "total_yuan", "groups", "priced", "top_traces"} <= set(data)

    def test_admin_costs_rejects_bad_group_by(self, client):
        """group_by 走白名单：不得任意取 CostRecord 属性（那会变成信息探测面）。"""
        r = client.get("/admin/costs?group_by=password")
        # 本项目把参数校验失败统一映射为 400（而非 FastAPI 默认 422）
        assert r.status_code in (400, 422), f"group_by 应受白名单约束，实得 {r.status_code}"
