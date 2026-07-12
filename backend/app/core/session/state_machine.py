"""对话状态机。

管理用户对话的生命周期状态流转，基于有限状态机(FSM)模式。
归属于L3会话管理层。

使用示例:
    from app.core.session import DialogueStateMachine, DialogueEvent

    fsm = DialogueStateMachine(redis_client)
    await fsm.transition("session_001", DialogueEvent.QUERY)
    state = await fsm.get_state("session_001")
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Optional

from app.core.logger import get_logger

logger = get_logger(__name__)


class DialogueState(str, Enum):
    """对话状态枚举。"""

    IDLE = "idle"                       # 空闲/初始状态
    PROCESSING = "processing"           # 处理中（AI正在响应）
    AWAITING_INPUT = "awaiting_input"   # 等待用户输入
    UPLOADING = "uploading"             # 上传文件中
    CONFIRMING = "confirming"           # 等待用户确认
    ERROR = "error"                     # 错误状态
    CLOSED = "closed"                   # 已关闭


class DialogueEvent(str, Enum):
    """对话事件枚举。"""

    QUERY = "query"                     # 用户发起查询
    RESPONSE = "response"               # AI响应完成
    UPLOAD = "upload"                   # 上传文件
    COMPLETE = "complete"               # 上传完成
    CANCEL = "cancel"                   # 取消操作
    CONFIRM = "confirm"                 # 用户确认
    REJECT = "reject"                   # 用户拒绝
    RETRY = "retry"                     # 重试
    ERROR_OCCURRED = "error_occurred"   # 发生错误
    CLOSE = "close"                     # 关闭会话
    TIMEOUT = "timeout"                 # 超时


# 状态转换表: {当前状态: {事件: 目标状态}}
_TRANSITION_TABLE: Dict[DialogueState, Dict[DialogueEvent, DialogueState]] = {
    DialogueState.IDLE: {
        DialogueEvent.QUERY: DialogueState.PROCESSING,
        DialogueEvent.UPLOAD: DialogueState.UPLOADING,
        DialogueEvent.CLOSE: DialogueState.CLOSED,
    },
    DialogueState.PROCESSING: {
        DialogueEvent.RESPONSE: DialogueState.AWAITING_INPUT,
        DialogueEvent.ERROR_OCCURRED: DialogueState.ERROR,
        DialogueEvent.CANCEL: DialogueState.IDLE,
        DialogueEvent.TIMEOUT: DialogueState.IDLE,
    },
    DialogueState.AWAITING_INPUT: {
        DialogueEvent.QUERY: DialogueState.PROCESSING,
        DialogueEvent.UPLOAD: DialogueState.UPLOADING,
        DialogueEvent.CONFIRM: DialogueState.IDLE,
        DialogueEvent.REJECT: DialogueState.PROCESSING,
        DialogueEvent.CLOSE: DialogueState.CLOSED,
        DialogueEvent.TIMEOUT: DialogueState.IDLE,
    },
    DialogueState.UPLOADING: {
        DialogueEvent.COMPLETE: DialogueState.PROCESSING,
        DialogueEvent.ERROR_OCCURRED: DialogueState.ERROR,
        DialogueEvent.CANCEL: DialogueState.IDLE,
    },
    DialogueState.CONFIRMING: {
        DialogueEvent.CONFIRM: DialogueState.IDLE,
        DialogueEvent.REJECT: DialogueState.PROCESSING,
        DialogueEvent.TIMEOUT: DialogueState.IDLE,
    },
    DialogueState.ERROR: {
        DialogueEvent.RETRY: DialogueState.PROCESSING,
        DialogueEvent.CANCEL: DialogueState.IDLE,
        DialogueEvent.CLOSE: DialogueState.CLOSED,
    },
    DialogueState.CLOSED: {},  # 终态，不可转换
}


_INVERSE_TRANSITIONS: Dict[DialogueState, Dict[DialogueState, DialogueEvent]] = {
    target: {src: evt for evt, src in mapping.items() if isinstance(src, DialogueState)}
    for target, mapping in _TRANSITION_TABLE.items()
    # Note: simplified inverse; used for validation
}


class DialogueStateMachine:
    """对话状态机。

    基于FSM模式管理对话状态流转。
    使用Redis存储状态数据，支持TTL自动过期。

    Attributes:
        redis: Redis异步客户端
        ttl: 状态TTL（秒）
    """

    def __init__(self, redis=None, ttl: int = 1800) -> None:
        """初始化对话状态机。

        Args:
            redis: Redis异步客户端实例。None时使用内存模式。
            ttl: 状态缓存TTL（秒），默认30分钟。
        """
        self._redis = redis
        self._ttl = ttl
        self._memory_store: Dict[str, str] = {} if redis is None else None

    async def get_state(self, session_id: str) -> DialogueState:
        """获取当前对话状态。

        Args:
            session_id: 会话ID。

        Returns:
            当前对话状态，默认为IDLE。
        """
        state_str: Optional[str] = None

        if self._redis is not None:
            state_str = await self._redis.get(f"session:{session_id}:state")
        elif self._memory_store is not None:
            state_str = self._memory_store.get(f"session:{session_id}:state")

        if state_str is None:
            return DialogueState.IDLE

        try:
            return DialogueState(state_str)
        except ValueError:
            logger.warning(f"无效的状态值: {state_str}，回退到IDLE")
            return DialogueState.IDLE

    async def transition(
        self,
        session_id: str,
        event: DialogueEvent,
    ) -> DialogueState:
        """执行状态转移。

        Args:
            session_id: 会话ID。
            event: 触发事件。

        Returns:
            转移后的状态。

        Raises:
            ValueError: 当当前状态下不允许该事件时。
        """
        current_state = await self.get_state(session_id)
        new_state = self._transition_table.get(current_state, {}).get(event)

        if new_state is None:
            raise ValueError(
                f"状态转换不允许: {current_state.value} -> {event.value} "
                f"(session_id={session_id})"
            )

        await self._persist_state(session_id, new_state)
        logger.debug(
            f"状态转换: {current_state.value} -> {new_state.value} "
            f"(event={event.value}, session={session_id})"
        )
        return new_state

    async def reset_state(self, session_id: str) -> None:
        """重置状态到IDLE。

        Args:
            session_id: 会话ID。
        """
        await self._persist_state(session_id, DialogueState.IDLE)
        logger.debug(f"状态重置: session={session_id}")

    async def clear_state(self, session_id: str) -> None:
        """清除状态数据。

        Args:
            session_id: 会话ID。
        """
        key = f"session:{session_id}:state"
        if self._redis is not None:
            await self._redis.delete(key)
        elif self._memory_store is not None:
            self._memory_store.pop(key, None)

    def _get_valid_events(self, state: DialogueState) -> list[DialogueEvent]:
        """获取指定状态下允许的事件列表。

        Args:
            state: 当前状态。

        Returns:
            允许的事件列表。
        """
        return list(self._transition_table.get(state, {}).keys())

    async def _persist_state(self, session_id: str, state: DialogueState) -> None:
        """持久化状态。

        Args:
            session_id: 会话ID。
            state: 目标状态。
        """
        key = f"session:{session_id}:state"
        value = state.value

        if self._redis is not None:
            await self._redis.set(key, value, ex=self._ttl)
        elif self._memory_store is not None:
            self._memory_store[key] = value

    @property
    def _transition_table(self) -> Dict[DialogueState, Dict[DialogueEvent, DialogueState]]:
        return _TRANSITION_TABLE
