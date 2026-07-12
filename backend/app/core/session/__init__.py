"""会话管理层。

提供对话状态机、会话管理器等核心会话管理能力。
归属于L3会话管理层。
"""

from app.core.session.state_machine import DialogueStateMachine, DialogueState, DialogueEvent
from app.core.session.manager import SessionManager

__all__ = [
    "DialogueStateMachine",
    "DialogueState",
    "DialogueEvent",
    "SessionManager",
]
