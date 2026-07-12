"""Agent流程调度器。

封装现有WorkflowEngine和QAAgent，实现多Agent编排与调度。
归属于L5核心决策层。

使用示例:
    from app.agents.orchestrator import agent_orchestrator

    result = await agent_orchestrator.orchestrate(
        intent="product_inquiry",
        context={"query": "产品的价格是多少？", "session_id": "xxx"},
    )
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any, Dict, List, Optional, Callable, Awaitable

from app.agents.qa_agent import get_default_agent
from app.workflow.engine import dag_engine
from app.workflow.models import WorkflowConfig, WorkflowNode, WorkflowEdge, NodeType
from app.core.logger import get_logger

logger = get_logger(__name__)


class AgentIntent(str, Enum):
    """Agent意图枚举。"""
    PRODUCT_INQUIRY = "product_inquiry"       # 产品咨询
    PRICE_COMPARE = "price_compare"            # 价格对比
    CUSTOMER_QUERY = "customer_query"          # 客户查询
    ORDER_RELATED = "order_related"            # 订单相关
    KNOWLEDGE_SEARCH = "knowledge_search"      # 知识检索
    GENERAL_CHAT = "general_chat"              # 通用对话


# 意图到Agent链的映射
_INTENT_AGENT_MAP: Dict[AgentIntent, List[str]] = {
    AgentIntent.PRODUCT_INQUIRY: ["rag_search", "response_generator"],
    AgentIntent.PRICE_COMPARE: ["rag_search", "comparison_builder"],
    AgentIntent.CUSTOMER_QUERY: ["rag_search", "customer_retriever", "response_generator"],
    AgentIntent.ORDER_RELATED: ["rag_search", "order_checker"],
    AgentIntent.KNOWLEDGE_SEARCH: ["rag_search"],
    AgentIntent.GENERAL_CHAT: ["response_generator"],
}


class AgentOrchestrator:
    """Agent流程调度器。

    根据用户意图选择合适的Agent链，编排执行流程。
    下游封装了WorkflowEngine和QAAgent。
    """

    def __init__(self) -> None:
        self._qa_agent = None
        self._workflow_engine = dag_engine

    async def orchestrate(
        self,
        intent: str,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """编排Agent执行流程。

        Args:
            intent: 意图标识。
            context: 对话上下文（包含query, session_id, collection_name等）。

        Returns:
            Agent执行结果。
        """
        intent_enum = self._resolve_intent(intent)
        query = context.get("query", "")
        session_id = context.get("session_id", "default")
        collection_name = context.get("collection_name", "default")

        logger.info(
            f"Agent编排: intent={intent_enum.value}, "
            f"session_id={session_id}"
        )

        # 使用QAAgent直接处理（复用已有实现）
        agent = await self._get_qa_agent()
        result = await agent.chat(
            user_input=query,
            session_id=session_id,
            collection_name=collection_name,
        )

        return {
            "intent": intent_enum.value,
            **result,
        }

    async def execute_workflow(
        self,
        config: WorkflowConfig,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """执行DAG工作流。

        Args:
            config: 工作流配置。
            context: 执行上下文。

        Returns:
            工作流执行结果。
        """
        execution = await self._workflow_engine.execute(config, context or {})
        return {
            "execution_id": execution.id,
            "status": execution.status.value,
            "results": execution.node_results if hasattr(execution, "node_results") else {},
        }

    def register_agent_intent(
        self,
        intent: str,
        agent_chain: List[str],
    ) -> None:
        """注册自定义意图的Agent链。

        Args:
            intent: 意图标识。
            agent_chain: Agent名称列表（执行顺序）。
        """
        try:
            intent_enum = AgentIntent(intent)
        except ValueError:
            logger.warning(f"未知意图类型: {intent}，使用自定义映射")
            _INTENT_AGENT_MAP[intent] = agent_chain  # type: ignore
            return
        _INTENT_AGENT_MAP[intent_enum] = agent_chain

    def _resolve_intent(self, intent: str) -> AgentIntent:
        """解析意图字符串。

        Args:
            intent: 意图标识。

        Returns:
            标准意图枚举。
        """
        try:
            return AgentIntent(intent)
        except ValueError:
            return AgentIntent.GENERAL_CHAT

    async def _get_qa_agent(self):
        """获取或创建QAAgent实例。"""
        if self._qa_agent is None:
            self._qa_agent = get_default_agent()
        return self._qa_agent


# 全局单例
agent_orchestrator = AgentOrchestrator()
