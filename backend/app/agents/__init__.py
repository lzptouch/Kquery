"""Agent模块 - 智能问答系统（延迟初始化）。

采用延迟初始化模式减少启动时的资源消耗。
Agent / Tool 仅在首次实际使用时创建。
"""

import threading
from typing import Optional

_lock = threading.Lock()

_rag_tool: Optional["RAGTool"] = None
_qa_agent: Optional["QAAgent"] = None
_conversation_memory: Optional["ConversationMemory"] = None


def get_rag_tool() -> "RAGTool":
    """延迟初始化获取 RAGTool（线程安全）"""
    global _rag_tool
    if _rag_tool is None:
        with _lock:
            if _rag_tool is None:
                from app.agents.rag_tool import RAGTool
                _rag_tool = RAGTool()
    return _rag_tool


def get_qa_agent() -> "QAAgent":
    """延迟初始化获取 QAAgent（线程安全）"""
    global _qa_agent
    if _qa_agent is None:
        with _lock:
            if _qa_agent is None:
                from app.agents.qa_agent import QAAgent
                _qa_agent = QAAgent()
    return _qa_agent


def get_conversation_memory() -> "ConversationMemory":
    """延迟初始化获取 ConversationMemory（线程安全）"""
    global _conversation_memory
    if _conversation_memory is None:
        with _lock:
            if _conversation_memory is None:
                from app.agents.memory import ConversationMemory
                _conversation_memory = ConversationMemory()
    return _conversation_memory


__all__ = [
    "RAGTool",
    "QAAgent",
    "ConversationMemory",
    "get_rag_tool",
    "get_qa_agent",
    "get_conversation_memory",
]