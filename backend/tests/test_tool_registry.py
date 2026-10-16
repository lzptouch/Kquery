"""工具注册表治理能力测试（批次 D）。

覆盖：
- TASK-125 危险级与写操作审批（只读工具出错最多答错，写工具会改外部系统）
- TASK-126 取消（取消后不再产生新的工具调用）
- TASK-109 健康熔断（连续失败摘除 + 半开恢复 + 目录标注不可用）
- DEF-A-17 空注册表兜底（此前抛裸 StopIteration）
"""
from app.services.agent.tools import ToolRegistry, ToolResult


def _evidence(chunk_id: str, score: float = 0.5) -> dict:
    return {
        "chunk_id": chunk_id, "content": "内容", "score": score,
        "source": "测试文档", "doc_id": "doc-t", "source_route": "vector",
    }


class _EchoTool:
    """记录被调用的查询串（用于断言"有没有真的执行"）。"""

    name = "vector_search"
    description = "记录调用"

    def __init__(self, log: list, error: str = "", danger: str = ""):
        self._log = log
        self._error = error
        if danger:
            self.danger = danger

    def run(self, query: str, tenant_id: str) -> ToolResult:
        self._log.append(query)
        if self._error:
            return ToolResult(error=self._error)
        return ToolResult(items=[_evidence("c#1")])


class _WriteTool:
    name = "http_request"
    description = "外部写操作"

    def __init__(self, log: list):
        self._log = log

    def run(self, query: str, tenant_id: str) -> ToolResult:
        self._log.append(query)
        return ToolResult(items=[_evidence("c#1")])


class TestToolDangerLevels:
    """TASK-125：危险级判定。"""

    def test_known_tools_mapped(self):
        from app.services.agent.tools import DANGER_READ_ONLY, DANGER_SAFE

        class Calc:
            name = "calculator"
            description = "计算"

        class Vec:
            name = "vector_search"
            description = "检索"

        assert ToolRegistry.danger_of(Calc()) == DANGER_SAFE
        assert ToolRegistry.danger_of(Vec()) == DANGER_READ_ONLY

    def test_unknown_tool_is_write(self):
        """未知来源工具按最危险处理——默认放行未知工具是权限事故的常见成因。"""
        from app.services.agent.tools import DANGER_WRITE

        class Mystery:
            name = "some_unknown_tool"
            description = "?"

        assert ToolRegistry.danger_of(Mystery()) == DANGER_WRITE

    def test_mcp_tools_are_write(self):
        """MCP 外部工具来源不可控，不能默认可信（TASK-127）。"""
        from app.services.agent.tools import DANGER_WRITE

        class Mcp:
            name = "mcp__fs__write_file"
            description = "外部工具"

        assert ToolRegistry.danger_of(Mcp()) == DANGER_WRITE

    def test_explicit_danger_attribute_wins(self):
        from app.services.agent.tools import DANGER_SAFE

        class Declared:
            name = "vector_search"  # 查表是 read_only
            description = "自定义"
            danger = DANGER_SAFE

        assert ToolRegistry.danger_of(Declared()) == DANGER_SAFE


class TestWriteApprovalGate:
    """TASK-125：写类工具必须先审批。"""

    def test_write_tool_blocked_without_approval(self):
        log: list = []
        reg = ToolRegistry(timeout_s=2)
        reg.register(_WriteTool(log))

        result, _, name = reg.run_with_timeout("http_request", "q", "t1")

        assert log == [], "未审批的写工具**绝不能被执行**"
        assert "审批" in result.error
        assert name == "http_request"

    def test_write_tool_runs_after_approval(self):
        log: list = []
        reg = ToolRegistry(timeout_s=2)
        reg.register(_WriteTool(log))

        result, _, _ = reg.run_with_timeout(
            "http_request", "q", "t1", approved={"http_request": True}
        )

        assert log == ["q"]
        assert not result.error

    def test_read_only_tool_needs_no_approval(self):
        log: list = []
        reg = ToolRegistry(timeout_s=2)
        reg.register(_EchoTool(log))
        result, _, _ = reg.run_with_timeout("vector_search", "q", "t1")
        assert log == ["q"] and not result.error


class TestToolCancellation:
    """TASK-126：取消后不再产生新的工具调用。"""

    def test_cancelled_run_skips_tool(self):
        log: list = []
        reg = ToolRegistry(timeout_s=2)
        reg.register(_EchoTool(log))
        reg.cancel("run-1")

        result, _, _ = reg.run_with_timeout("vector_search", "q", "t1", run_id="run-1")

        assert log == [], "已取消的 run 不得再触发工具执行"
        assert "取消" in result.error

    def test_other_run_unaffected(self):
        log: list = []
        reg = ToolRegistry(timeout_s=2)
        reg.register(_EchoTool(log))
        reg.cancel("run-1")

        result, _, _ = reg.run_with_timeout("vector_search", "q", "t1", run_id="run-2")

        assert log == ["q"] and not result.error

    def test_clear_cancel_restores(self):
        log: list = []
        reg = ToolRegistry(timeout_s=2)
        reg.register(_EchoTool(log))
        reg.cancel("run-1")
        reg.clear_cancel("run-1")

        assert reg.is_cancelled("run-1") is False
        result, _, _ = reg.run_with_timeout("vector_search", "q", "t1", run_id="run-1")
        assert not result.error

    def test_no_run_id_never_cancelled(self):
        log: list = []
        reg = ToolRegistry(timeout_s=2)
        reg.register(_EchoTool(log))
        result, _, _ = reg.run_with_timeout("vector_search", "q", "t1", run_id="")
        assert log == ["q"]


class TestToolHealthBreaker:
    """TASK-109：连续失败摘除 + 半开恢复。"""

    def _registry(self, fail_ref):
        def factory():
            return _EchoTool([], error="boom" if fail_ref[0] else "")

        reg = ToolRegistry(timeout_s=2)
        reg.register(factory())
        return reg, factory

    def test_consecutive_failures_trip_breaker(self):
        from app.services.agent.tools import HEALTH_FAIL_THRESHOLD

        reg, _ = self._registry([True])
        for _ in range(HEALTH_FAIL_THRESHOLD):
            reg.run_with_timeout("vector_search", "q", "t1")
        h = reg.health("vector_search")
        assert h["tripped"] is True, "连续失败达阈值应摘除"
        assert h["consecutive_fail"] >= HEALTH_FAIL_THRESHOLD

    def test_tripped_tool_marked_unavailable_in_catalog(self):
        from app.services.agent.tools import HEALTH_FAIL_THRESHOLD

        reg, _ = self._registry([True])
        for _ in range(HEALTH_FAIL_THRESHOLD):
            reg.run_with_timeout("vector_search", "q", "t1")
        entry = next(c for c in reg.catalog() if c["name"] == "vector_search")
        assert entry["available"] is False, "摘除的工具应在目录中标为不可用（让模型换工具）"

    def test_success_resets_consecutive_failures(self):
        log: list = []
        state = {"fail": True}

        class Flap:
            name = "vector_search"
            description = "抖动"

            def run(self, query, tenant_id):
                return ToolResult(error="boom") if state["fail"] else ToolResult(
                    items=[_evidence("c#1")]
                )

        reg = ToolRegistry(timeout_s=2)
        reg.register(Flap())
        for _ in range(3):
            reg.run_with_timeout("vector_search", "q", "t1")
        assert reg.health("vector_search")["consecutive_fail"] == 3

        state["fail"] = False
        reg.run_with_timeout("vector_search", "q", "t1")
        assert reg.health("vector_search")["consecutive_fail"] == 0

    def test_recovery_requires_multiple_successes(self):
        """半开恢复：偶发成功不能让抖动工具反复上下线。"""
        from app.services.agent.tools import HEALTH_FAIL_THRESHOLD, HEALTH_RECOVER_SUCCESS

        state = {"fail": True}

        class Flap:
            name = "vector_search"
            description = "抖动"

            def run(self, query, tenant_id):
                return ToolResult(error="boom") if state["fail"] else ToolResult(
                    items=[_evidence("c#1")]
                )

        reg = ToolRegistry(timeout_s=2)
        reg.register(Flap())
        for _ in range(HEALTH_FAIL_THRESHOLD):
            reg.run_with_timeout("vector_search", "q", "t1")
        assert reg.health("vector_search")["tripped"] is True

        state["fail"] = False
        for _ in range(HEALTH_RECOVER_SUCCESS - 1):
            reg.run_with_timeout("vector_search", "q", "t1")
        assert reg.health("vector_search")["tripped"] is True, "成功次数不足不应恢复"

        reg.run_with_timeout("vector_search", "q", "t1")
        assert reg.health("vector_search")["tripped"] is False, "达到恢复阈值应重新挂载"

    def test_health_report_shape(self):
        reg, _ = self._registry([True])
        rows = reg.health_report()
        assert rows and {"name", "tripped", "danger"} <= set(rows[0])

    def test_timeout_counts_as_failure(self):
        import time as _t

        class Slow:
            name = "vector_search"
            description = "慢"

            def run(self, query, tenant_id):
                _t.sleep(5)
                return ToolResult(items=[_evidence("c#1")])

        reg = ToolRegistry(timeout_s=1)
        reg.register(Slow())
        reg.run_with_timeout("vector_search", "q", "t1")
        assert reg.health("vector_search")["fail"] == 1, "超时也算失败（它没给出信息）"


class TestEmptyRegistryGuard:
    """DEF-A-17：空注册表不得抛裸 StopIteration。"""

    def test_resolve_empty_registry_raises_clear_error(self):
        import pytest

        reg = ToolRegistry()
        with pytest.raises(RuntimeError, match="注册表为空"):
            reg.resolve("whatever")

    def test_catalog_includes_availability_and_danger(self):
        log: list = []
        reg = ToolRegistry(timeout_s=2)
        reg.register(_EchoTool(log))
        entry = reg.catalog()[0]
        assert {"name", "description", "danger", "available"} <= set(entry)
        assert entry["available"] is True
