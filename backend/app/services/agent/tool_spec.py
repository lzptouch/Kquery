"""工具规格与结构化入参。

## 问题

`Tool` 协议此前只有 `run(query: str, tenant_id: str)` 两个位置参数：

- **模型不知道工具能接受什么参数**——目录里只有 `name` + `description`，
  于是"查 top_k=8、限定 doc_x、只看表格"这类意图完全无法表达；
- **步骤描述被原样当查询串**（`graph.py` 传 `step["description"]`），
  等于让 LLM 把参数"写进句子里"，抽取全靠下游猜；
- **入参校验缺失**：`top_k=99999` 会被照单全收，直到检索层自己崩。

## 本模块提供

- `ToolSpec`：工具的**自描述**（名称、用途、JSON Schema 入参、返回形态、危险级）；
- `ToolContext`：一次调用的**环境**（租户、ACL、审批结果等）。
  工具不再自行从参数里取租户，而是从 context 读——这是修复
  「SqlSearchTool 忽略 tenant_id」那类缺陷的结构性前提；
- `validate_arguments()`：入参校验，失败返回**错误文案**而非抛异常——
  文案会回传给 LLM 让它自我修正（这是 agentic 循环能自愈的关键）。

## 兼容策略

老工具不声明 `parameters` 时，自动退化为 `{query: str}` 的通用 schema。
这样改造可以逐个工具推进，不需要一次性重写全部工具与调用方。
"""
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError
from app.core.logging_setup import get_logger

logger = get_logger(__name__)

DANGER_SAFE = "safe"
DANGER_READ_ONLY = "read_only"
DANGER_WRITE = "write"


class QueryArgs(BaseModel):
    """通用入参（未声明 schema 的工具的默认形态）。"""

    query: str = Field(description="检索/计算的输入")
    top_k: int | None = Field(default=None, description="返回条数（不填则用默认值）")


@dataclass
class ToolContext:
    """一次工具调用的环境。

    工具通过它取租户与用户，而不是从自己的入参里读——入参是**模型可控**的，
    环境是**系统注入**的。把两者混在一起，模型就能通过构造入参越权
    （这正是 `SqlSearchTool` 之前"接收 tenant_id 却从不使用"埋下的隐患）。

    认证与授权已下线，`acl_filter` 一并移除：工具结果不再做文档级可见性过滤。
    """

    tenant_id: str = "default"
    user_id: str = "anon"
    run_id: str = ""
    trace_id: str = ""
    approved_tools: dict[str, bool] = field(default_factory=dict)
    extra: dict = field(default_factory=dict)


class ToolSpec(BaseModel):
    """工具的对外自描述（供目录展示与规划使用）。"""

    name: str
    description: str
    parameters: dict = Field(default_factory=dict)  # JSON Schema
    returns: str = ""
    danger: str = DANGER_READ_ONLY
    # 是否为新增能力（前端可据此打"新"标；也便于评估改造覆盖率）
    version: str = "1"


def schema_of(tool) -> dict:
    """取工具的入参 JSON Schema。

    三种来源，按优先级：
    1. 工具类属性 `arguments_model`（pydantic 模型）→ 自动生成 schema；
    2. 工具类属性 `parameters`（手写 dict）→ 原样返回；
    3. 都没有 → 通用 `{query}` schema（老工具兼容路径）。
    """
    model = getattr(tool, "arguments_model", None)
    if model is not None and isinstance(model, type) and issubclass(model, BaseModel):
        try:
            return model.model_json_schema()
        except Exception as exc:  # noqa: BLE001 - 工具 %s 的 schema 生成失败
            logger.warning("工具 %s 的 schema 生成失败（退回通用 schema）: %s",
                           getattr(tool, "name", "?"), exc)
    declared = getattr(tool, "parameters", None)
    if isinstance(declared, dict) and declared:
        return declared
    return QueryArgs.model_json_schema()


def spec_of(tool, danger_resolver=None) -> ToolSpec:
    """构造工具的完整规格。`danger_resolver` 用于复用注册表的危险级判定。"""
    danger = getattr(tool, "danger", None)
    if danger not in (DANGER_SAFE, DANGER_READ_ONLY, DANGER_WRITE):
        danger = (
            danger_resolver(tool) if callable(danger_resolver) else DANGER_READ_ONLY
        )
    return ToolSpec(
        name=getattr(tool, "name", ""),
        description=getattr(tool, "description", ""),
        parameters=schema_of(tool),
        returns=getattr(tool, "returns", ""),
        danger=danger,
        version=str(getattr(tool, "spec_version", "1")),
    )


def validate_arguments(tool, arguments: dict | None) -> tuple[dict, str]:
    """校验并规范化入参。

    返回 `(cleaned_arguments, error_message)`；`error_message` 非空表示校验失败。

    **刻意不抛异常**：校验失败的文案要回传给 LLM 让它修正参数重试——
    这是 agentic 循环自愈的关键路径。抛异常会让整步直接失败，
    而"参数写错"恰恰是模型最容易自我修正的一类错误。
    """
    arguments = dict(arguments or {})
    model = getattr(tool, "arguments_model", None)
    if model is not None and isinstance(model, type) and issubclass(model, BaseModel):
        try:
            validated = model(**arguments)
            return validated.model_dump(exclude_none=True), ""
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
            return {}, f"参数不合法（{problems}）。请修正后重试，或省略可选参数。"
    # 无 schema 的老工具：只做最基本的存在性检查
    if "query" in arguments and not str(arguments["query"]).strip():
        return {}, "query 不能为空"
    return arguments, ""


def arguments_from_step(step: dict) -> dict:
    """从计划步骤构造结构化入参。

    优先用步骤里显式的 `arguments`；没有则退回 `{query: description}`——
    这条兼容路径保证老计划（只写描述）仍然可用。
    """
    args = step.get("arguments")
    if isinstance(args, dict) and args:
        return dict(args)
    return {"query": step.get("description", "")}
