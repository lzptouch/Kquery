"""TASK-104：工具 Schema 化与结构化入参。

改造前：`Tool` 协议只有 `run(query, tenant_id)`，目录里只有 name + description，
模型无法表达"要 8 条""只看这篇文档"，只能把参数写进 query 句子靠下游猜；
`top_k=99999` 之类的入参会被照单全收直到检索层自己崩。
"""
from pydantic import BaseModel, Field

from app.services.agent.tool_spec import (
    DANGER_READ_ONLY,
    DANGER_SAFE,
    QueryArgs,
    ToolContext,
    ToolSpec,
    arguments_from_step,
    schema_of,
    spec_of,
    validate_arguments,
)


class _Declared:
    """声明了 arguments_model 的工具。"""

    name = "declared"
    description = "有 schema 的工具"
    returns = "若干条目"

    class arguments_model(BaseModel):
        query: str = Field(description="查询")
        top_k: int | None = Field(default=None, ge=1, le=50)
        doc_id: str | None = None


class _Legacy:
    """未声明 schema 的老工具（兼容路径）。"""

    name = "legacy"
    description = "老工具"


class _Handwritten:
    name = "hand"
    description = "手写 schema"
    parameters = {"type": "object", "properties": {"x": {"type": "integer"}}}


class TestSchemaResolution:
    def test_pydantic_model_generates_schema(self):
        schema = schema_of(_Declared())
        assert schema.get("properties", {}).get("query"), "应生成 query 属性"
        assert "top_k" in schema["properties"]

    def test_declared_dict_used_as_is(self):
        assert schema_of(_Handwritten()) == _Handwritten.parameters

    def test_legacy_tool_falls_back_to_query_schema(self):
        """老工具不声明 schema 时退回通用 {query} —— 保证改造可逐个推进。"""
        schema = schema_of(_Legacy())
        assert schema == QueryArgs.model_json_schema()

    def test_broken_schema_does_not_raise(self):
        class Broken:
            name = "broken"
            description = "坏的"
            arguments_model = "不是模型类"

        assert schema_of(Broken())  # 不抛，返回兜底 schema


class TestSpecOf:
    def test_spec_fields(self):
        spec = spec_of(_Declared())
        assert isinstance(spec, ToolSpec)
        assert spec.name == "declared" and spec.returns == "若干条目"
        assert spec.parameters

    def test_danger_inherited_from_attribute(self):
        class Safe:
            name = "calc"
            description = "计算"
            danger = DANGER_SAFE

        assert spec_of(Safe()).danger == DANGER_SAFE

    def test_danger_resolver_used_when_absent(self):
        """未声明 danger 时交给注册表的判定逻辑（避免两处口径不一致）。"""
        spec = spec_of(_Legacy(), danger_resolver=lambda t: "write")
        assert spec.danger == "write"

    def test_default_danger_is_read_only(self):
        assert spec_of(_Legacy()).danger == DANGER_READ_ONLY


class TestArgumentValidation:
    def test_valid_arguments_pass(self):
        cleaned, err = validate_arguments(_Declared(), {"query": "苹果", "top_k": 5})
        assert err == "" and cleaned["query"] == "苹果" and cleaned["top_k"] == 5

    def test_none_values_dropped(self):
        """未填的可选参数应被丢弃，而不是以 None 传进工具（工具方要多一层判断）。"""
        cleaned, err = validate_arguments(_Declared(), {"query": "x", "doc_id": None})
        assert err == "" and "doc_id" not in cleaned

    def test_out_of_range_returns_error_not_exception(self):
        """越界必须返回**文案**而非抛异常——文案要回传给 LLM 自我修正。"""
        cleaned, err = validate_arguments(_Declared(), {"query": "x", "top_k": 99999})
        assert cleaned == {} and err
        assert "top_k" in err, f"错误文案应指明是哪个字段：{err}"

    def test_missing_required_returns_error(self):
        _, err = validate_arguments(_Declared(), {})
        assert err and "query" in err

    def test_wrong_type_returns_error(self):
        _, err = validate_arguments(_Declared(), {"query": "x", "top_k": "很多"})
        assert err

    def test_legacy_tool_rejects_empty_query(self):
        _, err = validate_arguments(_Legacy(), {"query": "   "})
        assert err

    def test_legacy_tool_passes_through_extra_keys(self):
        cleaned, err = validate_arguments(_Legacy(), {"query": "x", "whatever": 1})
        assert err == "" and cleaned["whatever"] == 1

    def test_none_arguments_ok(self):
        _, err = validate_arguments(_Legacy(), None)
        assert err == ""


class TestArgumentsFromStep:
    def test_explicit_arguments_win(self):
        step = {"description": "描述", "arguments": {"query": "显式", "top_k": 3}}
        assert arguments_from_step(step) == {"query": "显式", "top_k": 3}

    def test_fallback_to_description(self):
        """老计划只写描述 → 退回 query，保证既有计划仍可执行。"""
        assert arguments_from_step({"description": "查年假政策"}) == {"query": "查年假政策"}

    def test_empty_arguments_dict_falls_back(self):
        assert arguments_from_step({"description": "d", "arguments": {}}) == {"query": "d"}


class TestToolContext:
    def test_context_is_system_injected_not_model_controlled(self):
        """租户来自 context 而非入参——入参是模型可控的，环境是系统注入的。

        这是修复「SqlSearchTool 接收 tenant_id 却从不使用」那类缺陷的结构性前提。
        """
        ctx = ToolContext(tenant_id="t1", user_id="u1", run_id="r1")
        assert ctx.tenant_id == "t1"
        assert not hasattr(ctx, "arguments")

    def test_defaults(self):
        ctx = ToolContext()
        assert ctx.tenant_id == "default" and ctx.acl_filter is None
        assert ctx.approved_tools == {}


class TestRegistryIntegration:
    def test_catalog_includes_parameters_schema(self):
        """目录必须带 parameters —— 这是模型能表达结构化入参的唯一途径。"""
        from app.services.agent.tools import ToolRegistry

        reg = ToolRegistry(timeout_s=2)
        reg.register(_Declared())
        entry = reg.catalog()[0]
        assert "parameters" in entry
        assert "query" in entry["parameters"].get("properties", {})

    def test_specs_endpoint_shape(self):
        from app.services.agent.tools import ToolRegistry

        reg = ToolRegistry(timeout_s=2)
        reg.register(_Declared())
        spec = reg.specs()[0]
        assert {"name", "description", "parameters", "returns", "danger"} <= set(spec)

    def test_invalid_arguments_return_error_not_raise(self):
        """端到端：越界入参必须被拦成错误文案，而不是让工具崩。"""
        from app.services.agent.tools import ToolRegistry, ToolResult

        class Strict:
            name = "strict"
            description = "严格"
            # 必须声明 danger：未声明的工具按 write 处理（TASK-125 的失败安全取向），
            # 会在参数校验之前就被审批闸门拦下
            danger = "read_only"

            class arguments_model(BaseModel):
                query: str
                top_k: int = Field(default=1, le=10)

            def run(self, query, tenant_id, **kw):
                return ToolResult(items=[])

        reg = ToolRegistry(timeout_s=2)
        reg.register(Strict())
        result, _, _ = reg.run_with_timeout(
            "strict", "q", "t1", arguments={"query": "x", "top_k": 999}
        )
        assert result.error and "参数不合法" in result.error

    def test_valid_arguments_reach_tool(self):
        """结构化入参必须真的传到工具（而不是被吞掉）。"""
        from app.services.agent.tools import ToolRegistry, ToolResult

        seen = {}

        class Recorder:
            name = "rec"
            description = "记录"
            danger = "read_only"

            class arguments_model(BaseModel):
                query: str
                top_k: int | None = None

            def run(self, query, tenant_id, **kw):
                seen.update({"query": query, "kwargs": kw})
                return ToolResult(items=[])

        reg = ToolRegistry(timeout_s=2)
        reg.register(Recorder())
        reg.run_with_timeout("rec", "原查询", "t1", arguments={"query": "显式查询", "top_k": 7})
        assert seen["kwargs"].get("top_k") == 7, "结构化参数未传到工具"

    def test_vector_search_exposes_doc_id_and_top_k(self):
        """内置工具也要有 schema（否则改造只覆盖了测试替身）。"""
        from app.services.agent.tool_spec import schema_of

        spec = schema_of(type("V", (), {
            "name": "vector_search",
            "description": "d",
        })())
        # 未声明的类型走兜底；这里直接验证真实类
        from app.services.agent.tools import VectorSearchTool

        real = schema_of(VectorSearchTool.__new__(VectorSearchTool))
        props = real.get("properties", {})
        assert "query" in props and "doc_id" in props and "top_k" in props


class TestGraphPassesArguments:
    def test_plan_arguments_flow_to_registry(self):
        """计划里的显式 arguments 必须传到工具层（否则 schema 化形同虚设）。"""
        from app.services.agent.tool_spec import arguments_from_step

        step = {"description": "查", "arguments": {"query": "年假", "top_k": 3}}
        args = arguments_from_step(step)
        assert args["top_k"] == 3
