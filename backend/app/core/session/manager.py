"""会话管理器。

管理用户会话的生命周期，包括创建、恢复、清理和心跳保活。
基于Redis存储会话数据，支持TTL自动过期。
归属于L3会话管理层。

使用示例:
    from app.core.session import SessionManager

    mgr = SessionManager(redis_client)
    session_id = await mgr.create_session(user_id="u123")
    session = await mgr.get_session(session_id)
    await mgr.refresh_session(session_id)
    await mgr.end_session(session_id)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.logger import get_logger
from app.core.session.state_machine import DialogueState, DialogueEvent

logger = get_logger(__name__)


@dataclass
class SessionData:
    """会话数据模型。

    Attributes:
        session_id: 会话唯一标识。
        user_id: 关联用户ID。
        created_at: 创建时间(ISO格式)。
        last_active_at: 最后活跃时间(ISO格式)。
        message_count: 消息数量。
        state: 当前对话状态。
        collection_name: 关联的知识库名称。
        metadata: 自定义元数据。
        context_summary: 对话上下文摘要。
    """

    session_id: str
    user_id: Optional[str] = None
    created_at: str = ""
    last_active_at: str = ""
    message_count: int = 0
    state: str = DialogueState.IDLE.value
    collection_name: str = "default"
    metadata: Dict[str, Any] = field(default_factory=dict)
    context_summary: str = ""


_DEFAULT_TTL = 7200  # 2小时


class SessionManager:
    """会话管理器。

    管理用户会话的完整生命周期，使用Redis存储。

    Attributes:
        redis: Redis异步客户端实例。
        ttl: 会话TTL（秒）。
    """

    def __init__(self, redis=None, ttl: int = _DEFAULT_TTL) -> None:
        """初始化会话管理器。

        Args:
            redis: Redis异步客户端实例。None时使用内存模式。
            ttl: 会话TTL（秒），默认2小时。
        """
        self._redis = redis
        self._ttl = ttl
        self._memory_store: Dict[str, str] = {} if redis is None else None

    async def create_session(
        self,
        user_id: str = "anonymous",
        collection_name: str = "default",
        metadata: Optional[Dict[str, Any]] = None,
        ttl: Optional[int] = None,
    ) -> str:
        """创建新会话。

        Args:
            user_id: 用户ID，匿名会话默认为"anonymous"。
            collection_name: 关联知识库名称。
            metadata: 自定义元数据。
            ttl: 会话TTL（秒），默认使用实例TTL。

        Returns:
            创建的会话ID。
        """
        session_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()

        session = SessionData(
            session_id=session_id,
            user_id=user_id,
            created_at=now,
            last_active_at=now,
            collection_name=collection_name,
            metadata=metadata or {},
        )

        await self._save_session(session, ttl)
        logger.info(
            f"会话已创建: session_id={session_id}, "
            f"user_id={user_id}, collection={collection_name}"
        )
        return session_id

    async def get_session(self, session_id: str) -> Optional[SessionData]:
        """获取会话信息。

        Args:
            session_id: 会话ID。

        Returns:
            会话数据对象，不存在时返回None。
        """
        data_str: Optional[str] = None

        if self._redis is not None:
            data_str = await self._redis.get(f"session:{session_id}")
        elif self._memory_store is not None:
            data_str = self._memory_store.get(f"session:{session_id}")

        if data_str is None:
            return None

        try:
            data = json.loads(data_str)
            if isinstance(data, dict):
                metadata = data.pop("metadata", {})
                session = SessionData(**data)
                session.metadata = metadata if isinstance(metadata, dict) else {}
                return session
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.error(f"会话数据解析失败: session_id={session_id}, error={e}")

        return None

    async def end_session(self, session_id: str) -> bool:
        """结束/删除会话。

        Args:
            session_id: 会话ID。

        Returns:
            是否成功删除。
        """
        key = f"session:{session_id}"
        if self._redis is not None:
            result = await self._redis.delete(key)
            await self._redis.delete(f"{key}:state")
            return result > 0
        elif self._memory_store is not None:
            self._memory_store.pop(key, None)
            self._memory_store.pop(f"{key}:state", None)
            return True
        return False

    async def refresh_session(self, session_id: str, ttl: Optional[int] = None) -> bool:
        """刷新会话TTL（心跳保活）。

        Args:
            session_id: 会话ID。
            ttl: 新的TTL（秒），默认使用实例TTL。

        Returns:
            是否成功刷新。
        """
        ttl = ttl or self._ttl
        key = f"session:{session_id}"

        if self._redis is not None:
            result = await self._redis.expire(key, ttl)
            if result:
                # 更新最后活跃时间
                await self._update_last_active(session_id)
            return result
        return True

    async def list_sessions(
        self,
        user_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[SessionData]:
        """列出所有活跃会话。

        Args:
            user_id: 可选的用户ID过滤，None 时返回所有会话。
            limit: 最大返回数量。

        Returns:
            会话列表（按最后活跃时间降序）。
        """
        sessions: List[SessionData] = []

        if self._memory_store is not None:
            for key, value in self._memory_store.items():
                if not key.startswith("session:") or key.endswith(":state"):
                    continue
                try:
                    data = json.loads(value)
                    if user_id is None or data.get("user_id") == user_id:
                        sessions.append(self._dict_to_session(data))
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue

        return sorted(
            sessions,
            key=lambda s: s.last_active_at or "",
            reverse=True,
        )[:limit]

    async def _save_session(self, session: SessionData, ttl: Optional[int] = None) -> None:
        """保存会话数据。

        Args:
            session: 会话数据对象。
            ttl: TTL（秒）。
        """
        key = f"session:{session.session_id}"
        value = json.dumps(asdict(session), ensure_ascii=False)
        expire = ttl or self._ttl

        if self._redis is not None:
            await self._redis.set(key, value, ex=expire)
        elif self._memory_store is not None:
            self._memory_store[key] = value

    async def _update_last_active(self, session_id: str) -> None:
        """更新最后活跃时间。

        Args:
            session_id: 会话ID。
        """
        session = await self.get_session(session_id)
        if session:
            session.last_active_at = datetime.utcnow().isoformat()
            await self._save_session(session)

    @staticmethod
    def _dict_to_session(data: Dict[str, Any]) -> SessionData:
        """将字典转换为SessionData对象。

        Args:
            data: 会话数据字典。

        Returns:
            SessionData对象。
        """
        metadata = data.pop("metadata", {}) or {}
        session = SessionData(**data)
        session.metadata = metadata if isinstance(metadata, dict) else {}
        return session
