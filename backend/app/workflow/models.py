"""工作流编排引擎 - 数据模型

定义工作流编排引擎的核心数据模型：
- WorkflowNode: 工作流节点
- WorkflowEdge: 工作流边
- WorkflowConfig: 工作流配置
- WorkflowExecution: 工作流执行实例
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union
from datetime import datetime
from uuid import uuid4


class NodeType(str, Enum):
    """工作流节点类型"""
    LLM = "llm"               # LLM 调用节点
    RETRIEVER = "retriever"    # 检索节点
    CONDITION = "condition"    # 条件判断节点
    OUTPUT = "output"          # 输出节点
    TRANSFORM = "transform"    # 数据转换节点
    TOOL = "tool"              # 工具调用节点
    START = "start"            # 开始节点
    END = "end"                # 结束节点


class ExecutionStatus(str, Enum):
    """执行状态"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class WorkflowNode:
    """工作流节点

    定义工作流中的一个处理节点，包含节点的类型、配置和输入输出定义。

    Attributes:
        id: 节点唯一标识
        name: 节点名称
        node_type: 节点类型
        config: 节点配置参数
        inputs: 输入参数定义
        outputs: 输出参数定义
        position: 节点在画布上的位置（用于前端可视化）
    """

    id: str
    name: str
    node_type: NodeType
    config: Dict[str, Any] = field(default_factory=dict)
    inputs: Dict[str, str] = field(default_factory=dict)
    outputs: Dict[str, str] = field(default_factory=dict)
    position: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "node_type": self.node_type.value,
            "config": self.config,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "position": self.position,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowNode":
        return cls(
            id=data["id"],
            name=data["name"],
            node_type=NodeType(data["node_type"]),
            config=data.get("config", {}),
            inputs=data.get("inputs", {}),
            outputs=data.get("outputs", {}),
            position=data.get("position", {}),
        )


@dataclass
class WorkflowEdge:
    """工作流边

    定义两个节点之间的连接关系。

    Attributes:
        id: 边唯一标识
        source: 源节点ID
        target: 目标节点ID
        condition: 条件表达式（用于条件分支）
        label: 边标签
    """

    id: str
    source: str
    target: str
    condition: Optional[str] = None
    label: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "condition": self.condition,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowEdge":
        return cls(
            id=data["id"],
            source=data["source"],
            target=data["target"],
            condition=data.get("condition"),
            label=data.get("label"),
        )


@dataclass
class WorkflowConfig:
    """工作流配置

    定义完整的工作流结构，包含节点、边和全局配置。

    Attributes:
        id: 工作流ID
        name: 工作流名称
        description: 工作流描述
        nodes: 节点列表
        edges: 边列表
        global_config: 全局配置参数
        created_at: 创建时间
        updated_at: 更新时间
    """

    id: str
    name: str
    description: str = ""
    nodes: List[WorkflowNode] = field(default_factory=list)
    edges: List[WorkflowEdge] = field(default_factory=list)
    global_config: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        if self.id is None:
            self.id = str(uuid4())
        if self.created_at is None:
            self.created_at = datetime.utcnow()

    def get_node(self, node_id: str) -> Optional[WorkflowNode]:
        """根据ID获取节点"""
        for node in self.nodes:
            if node.id == node_id:
                return node
        return None

    def get_next_nodes(self, node_id: str) -> List[WorkflowNode]:
        """获取指定节点的后继节点"""
        next_ids = [
            edge.target for edge in self.edges if edge.source == node_id
        ]
        return [n for n in self.nodes if n.id in next_ids]

    def get_prev_nodes(self, node_id: str) -> List[WorkflowNode]:
        """获取指定节点的前驱节点"""
        prev_ids = [
            edge.source for edge in self.edges if edge.target == node_id
        ]
        return [n for n in self.nodes if n.id in prev_ids]

    def get_start_node(self) -> Optional[WorkflowNode]:
        """获取开始节点"""
        for node in self.nodes:
            if node.node_type == NodeType.START:
                return node
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "global_config": self.global_config,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowConfig":
        return cls(
            id=data["id"],
            name=data["name"],
            description=data.get("description", ""),
            nodes=[WorkflowNode.from_dict(n) for n in data.get("nodes", [])],
            edges=[WorkflowEdge.from_dict(e) for e in data.get("edges", [])],
            global_config=data.get("global_config", {}),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at") else None
            ),
            updated_at=(
                datetime.fromisoformat(data["updated_at"])
                if data.get("updated_at") else None
            ),
        )


@dataclass
class NodeExecutionResult:
    """节点执行结果

    Attributes:
        node_id: 节点ID
        status: 执行状态
        output: 执行输出
        error: 错误信息
        started_at: 开始时间
        completed_at: 完成时间
        metadata: 额外元数据
    """

    node_id: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    output: Any = None
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> Optional[float]:
        """执行耗时（毫秒）"""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds() * 1000
        return None


@dataclass
class WorkflowExecution:
    """工作流执行实例

    Attributes:
        id: 执行实例ID
        workflow_id: 工作流ID
        status: 整体执行状态
        node_results: 节点执行结果映射
        inputs: 执行输入参数
        started_at: 开始时间
        completed_at: 完成时间
        error: 全局错误信息
    """

    id: str
    workflow_id: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    node_results: Dict[str, NodeExecutionResult] = field(default_factory=dict)
    inputs: Dict[str, Any] = field(default_factory=dict)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    error: Optional[str] = None

    def __post_init__(self):
        if self.id is None:
            self.id = str(uuid4())

    @property
    def duration_ms(self) -> Optional[float]:
        """执行耗时（毫秒）"""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds() * 1000
        return None

    def get_output(self, node_id: str) -> Optional[Any]:
        """获取指定节点的输出"""
        result = self.node_results.get(node_id)
        if result:
            return result.output
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "node_results": {
                k: {
                    "node_id": v.node_id,
                    "status": v.status.value,
                    "output": v.output,
                    "error": v.error,
                    "duration_ms": v.duration_ms,
                }
                for k, v in self.node_results.items()
            },
            "inputs": self.inputs,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }