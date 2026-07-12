"""工作流编排引擎

提供基于 DAG 的工作流编排能力，支持：
- 可视化工作流定义
- 多类型节点（LLM、检索、条件、工具等）
- 拓扑排序执行
- 条件分支和错误处理
- 执行上下文传递

模块结构：
- models: 数据模型定义
- engine: DAG 执行引擎
"""

from app.workflow.models import (
    WorkflowConfig,
    WorkflowNode,
    WorkflowEdge,
    WorkflowExecution,
    NodeExecutionResult,
    NodeType,
    ExecutionStatus,
)
from app.workflow.engine import (
    DAGExecutionEngine,
    NodeExecutor,
    dag_engine,
)

__all__ = [
    "WorkflowConfig",
    "WorkflowNode",
    "WorkflowEdge",
    "WorkflowExecution",
    "NodeExecutionResult",
    "NodeType",
    "ExecutionStatus",
    "DAGExecutionEngine",
    "NodeExecutor",
    "dag_engine",
]