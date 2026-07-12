"""工作流编排引擎 - DAG 执行引擎

基于有向无环图（DAG）的工作流执行引擎。
支持拓扑排序、并行执行、条件分支和错误处理。
"""

import asyncio
from collections import deque
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from app.workflow.models import (
    WorkflowConfig,
    WorkflowNode,
    WorkflowEdge,
    WorkflowExecution,
    NodeExecutionResult,
    NodeType,
    ExecutionStatus,
)
from app.core.logger import get_logger

logger = get_logger(__name__)


class NodeExecutor:
    """节点执行器

    负责执行单个工作流节点，根据节点类型调用相应的处理逻辑。
    """

    def __init__(self):
        self._handlers: Dict[NodeType, Callable] = {}

    def register_handler(
        self, node_type: NodeType, handler: Callable
    ) -> None:
        """注册节点类型处理器

        Args:
            node_type: 节点类型
            handler: 处理函数，签名为 async def handler(node, context) -> Any
        """
        self._handlers[node_type] = handler

    async def execute(
        self,
        node: WorkflowNode,
        context: Dict[str, Any],
    ) -> NodeExecutionResult:
        """执行单个节点

        Args:
            node: 要执行的节点
            context: 执行上下文（包含之前节点的输出）

        Returns:
            节点执行结果
        """
        result = NodeExecutionResult(
            node_id=node.id,
            status=ExecutionStatus.RUNNING,
            started_at=datetime.utcnow(),
        )

        try:
            handler = self._handlers.get(node.node_type)
            if handler is None:
                raise ValueError(f"未找到节点类型处理器: {node.node_type}")

            output = await handler(node, context)
            result.status = ExecutionStatus.COMPLETED
            result.output = output
            logger.info(f"节点执行成功: {node.name} ({node.id})")

        except Exception as e:
            result.status = ExecutionStatus.FAILED
            result.error = str(e)
            logger.error(f"节点执行失败: {node.name} ({node.id}), 错误: {e}")

        finally:
            result.completed_at = datetime.utcnow()

        return result


class DAGExecutionEngine:
    """DAG 工作流执行引擎

    基于拓扑排序的顺序执行工作流，支持：
    - 自动检测依赖关系
    - 拓扑排序确定执行顺序
    - 条件分支处理
    - 错误处理和节点跳过
    - 执行上下文传递

    使用示例:
        engine = DAGExecutionEngine(node_executor)

        config = WorkflowConfig(
            id="wf-1",
            name="问答工作流",
            nodes=[...],
            edges=[...],
        )

        execution = await engine.execute(config, inputs={"query": "什么是RAG?"})
    """

    def __init__(self, node_executor: Optional[NodeExecutor] = None):
        self._node_executor = node_executor or NodeExecutor()

    @property
    def node_executor(self) -> NodeExecutor:
        return self._node_executor

    async def execute(
        self,
        config: WorkflowConfig,
        inputs: Dict[str, Any] = None,
    ) -> WorkflowExecution:
        """执行工作流

        Args:
            config: 工作流配置
            inputs: 输入参数

        Returns:
            工作流执行实例
        """
        execution = WorkflowExecution(
            id=None,
            workflow_id=config.id,
            status=ExecutionStatus.RUNNING,
            inputs=inputs or {},
            started_at=datetime.utcnow(),
        )

        try:
            # 构建执行上下文
            context = {**(inputs or {})}

            # 获取拓扑排序后的节点列表
            sorted_nodes = self._topological_sort(config)

            # 按顺序执行节点
            for node in sorted_nodes:
                result = await self._node_executor.execute(node, context)

                # 存储执行结果
                execution.node_results[node.id] = result
                context[node.id] = result.output

                # 处理执行失败
                if result.status == ExecutionStatus.FAILED:
                    if not config.global_config.get("continue_on_error", False):
                        execution.status = ExecutionStatus.FAILED
                        execution.error = f"节点 {node.name} 执行失败: {result.error}"
                        logger.error(f"工作流执行失败: {execution.error}")
                        return execution

            execution.status = ExecutionStatus.COMPLETED
            logger.info(
                f"工作流执行成功: {config.name} ({config.id}), "
                f"共执行 {len(sorted_nodes)} 个节点"
            )

        except Exception as e:
            execution.status = ExecutionStatus.FAILED
            execution.error = str(e)
            logger.error(f"工作流执行异常: {config.name}, 错误: {e}")

        finally:
            execution.completed_at = datetime.utcnow()

        return execution

    def _topological_sort(
        self, config: WorkflowConfig
    ) -> List[WorkflowNode]:
        """拓扑排序

        使用 Kahn 算法对节点进行拓扑排序，确保依赖关系正确。

        Args:
            config: 工作流配置

        Returns:
            拓扑排序后的节点列表

        Raises:
            ValueError: 如果工作流中存在循环依赖
        """
        # 构建入度表和邻接表
        in_degree: Dict[str, int] = {node.id: 0 for node in config.nodes}
        adjacency: Dict[str, List[str]] = {node.id: [] for node in config.nodes}

        for edge in config.edges:
            if edge.source in adjacency:
                adjacency[edge.source].append(edge.target)
            if edge.target in in_degree:
                in_degree[edge.target] += 1

        # Kahn 算法
        queue = deque(
            [node_id for node_id, degree in in_degree.items() if degree == 0]
        )
        sorted_ids = []

        while queue:
            current = queue.popleft()
            sorted_ids.append(current)

            for neighbor in adjacency.get(current, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(sorted_ids) != len(config.nodes):
            raise ValueError("工作流中存在循环依赖，无法执行")

        # 按排序顺序返回节点
        node_map = {node.id: node for node in config.nodes}
        return [node_map[node_id] for node_id in sorted_ids]

    def validate_workflow(self, config: WorkflowConfig) -> Tuple[bool, Optional[str]]:
        """验证工作流配置的有效性

        Args:
            config: 工作流配置

        Returns:
            (是否有效, 错误信息)
        """
        if not config.nodes:
            return False, "工作流至少需要一个节点"

        if not config.edges:
            return False, "工作流至少需要一条边"

        # 检查是否有开始节点和结束节点
        node_types = {n.node_type for n in config.nodes}
        if NodeType.START not in node_types:
            return False, "工作流缺少开始节点 (START)"
        if NodeType.END not in node_types:
            return False, "工作流缺少结束节点 (END)"

        # 检查循环依赖
        try:
            self._topological_sort(config)
        except ValueError as e:
            return False, str(e)

        return True, None


# 默认节点处理器注册

async def _handle_llm_node(
    node: WorkflowNode, context: Dict[str, Any]
) -> Any:
    """处理 LLM 节点"""
    config = node.config
    messages = config.get("messages", [])
    model = config.get("model", "default")

    # 从上下文解析模板变量
    resolved_messages = []
    for msg in messages:
        content = msg.get("content", "")
        for key, value in context.items():
            content = content.replace(f"{{{{{key}}}}}", str(value))
        resolved_messages.append({"role": msg.get("role", "user"), "content": content})

    from app.plugins.registry import plugin_registry
    provider = plugin_registry.get_llm_provider(model)
    if provider:
        return await provider.chat(resolved_messages)

    return {"response": f"[LLM响应] 模型: {model}"}


async def _handle_retriever_node(
    node: WorkflowNode, context: Dict[str, Any]
) -> Any:
    """处理检索节点"""
    config = node.config
    query = config.get("query", "")
    top_k = config.get("top_k", 5)

    # 从上下文解析变量
    for key, value in context.items():
        query = query.replace(f"{{{{{key}}}}}", str(value))

    from app.plugins.registry import plugin_registry
    retriever_name = config.get("retriever", "default")
    retriever = plugin_registry.get_retriever(retriever_name)
    if retriever:
        return await retriever.search(query, top_k=top_k)

    return {"results": []}


async def _handle_condition_node(
    node: WorkflowNode, context: Dict[str, Any]
) -> Any:
    """处理条件判断节点"""
    config = node.config
    condition = config.get("condition", "true")

    # 安全地评估条件表达式
    try:
        safe_globals = {
            "__builtins__": {},
            "True": True,
            "False": False,
            "None": None,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
        }
        result = eval(condition, safe_globals, context)
        return {"result": bool(result), "branch": "true" if result else "false"}
    except Exception as e:
        logger.warning(f"条件表达式评估失败: {condition}, 错误: {e}")
        return {"result": False, "branch": "false"}


async def _handle_output_node(
    node: WorkflowNode, context: Dict[str, Any]
) -> Any:
    """处理输出节点"""
    config = node.config
    output_fields = config.get("fields", [])

    if output_fields:
        return {field: context.get(field) for field in output_fields}
    return context


async def _handle_transform_node(
    node: WorkflowNode, context: Dict[str, Any]
) -> Any:
    """处理数据转换节点"""
    config = node.config
    transform_type = config.get("transform_type", "passthrough")

    if transform_type == "passthrough":
        return context.get(config.get("input_key", ""))
    elif transform_type == "template":
        template = config.get("template", "")
        for key, value in context.items():
            template = template.replace(f"{{{{{key}}}}}", str(value))
        return {"result": template}

    return context


async def _handle_tool_node(
    node: WorkflowNode, context: Dict[str, Any]
) -> Any:
    """处理工具调用节点"""
    config = node.config
    tool_name = config.get("tool_name", "")
    params = config.get("params", {})

    # 从上下文解析参数
    resolved_params = {}
    for key, value in params.items():
        if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
            context_key = value[2:-2]
            resolved_params[key] = context.get(context_key, value)
        else:
            resolved_params[key] = value

    from app.plugins.registry import plugin_registry
    tool = plugin_registry.get_tool(tool_name)
    if tool:
        return await tool.execute(**resolved_params)

    return {"error": f"工具未找到: {tool_name}"}


# 创建默认节点执行器并注册处理器
_default_executor = NodeExecutor()
_default_executor.register_handler(NodeType.LLM, _handle_llm_node)
_default_executor.register_handler(NodeType.RETRIEVER, _handle_retriever_node)
_default_executor.register_handler(NodeType.CONDITION, _handle_condition_node)
_default_executor.register_handler(NodeType.OUTPUT, _handle_output_node)
_default_executor.register_handler(NodeType.TRANSFORM, _handle_transform_node)
_default_executor.register_handler(NodeType.TOOL, _handle_tool_node)
_default_executor.register_handler(NodeType.START, lambda n, c: c)
_default_executor.register_handler(NodeType.END, lambda n, c: c)


# 全局 DAG 执行引擎
dag_engine = DAGExecutionEngine(node_executor=_default_executor)