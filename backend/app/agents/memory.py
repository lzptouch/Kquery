"""对话记忆管理 - 管理Agent与用户的对话历史。

提供分布式对话上下文记忆功能，支持多轮对话的连贯性。
使用 Redis 作为主要存储后端，内存作为降级方案。
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime
import json

import redis.asyncio as aioredis

from app.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class ConversationMessage:
    """对话消息"""
    role: str  # "user" 或 "assistant"
    content: str
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


class ConversationMemory:
    """分布式对话记忆管理器（Redis 后端 + 内存降级）

    管理用户与Agent的对话历史，支持：
    - 存储多轮对话
    - 限制记忆长度
    - 检索相关对话历史
    - 会话管理
    - Redis 不可用时自动降级到内存存储
    """

    CHAT_PREFIX = "kquery:chat:"

    def __init__(
        self,
        redis_client: Optional[aioredis.Redis] = None,
        max_history_length: int = 20,
    ):
        """初始化对话记忆

        Args:
            redis_client: Redis 客户端实例（可选，会自动尝试从 cache 模块获取）
            max_history_length: 最大对话历史长度
        """
        self.max_history_length = max_history_length
        self._redis = redis_client
        self._fallback: Dict[str, List[ConversationMessage]] = {}

    async def _ensure_redis(self) -> bool:
        """确保 Redis 可用"""
        if self._redis is None:
            try:
                from app.core.cache import redis_cache_service
                if redis_cache_service._initialized:
                    self._redis = redis_cache_service.redis
            except Exception:
                pass
        return self._redis is not None

    def _session_key(self, session_id: str) -> str:
        """生成 Redis 会话键"""
        return f"{self.CHAT_PREFIX}{session_id}"

    async def create_session(self, session_id: str, ttl: int = 86400 * 7) -> None:
        """创建新的对话会话

        Args:
            session_id: 会话ID
            ttl: 会话过期时间（秒），默认 7 天
        """
        if await self._ensure_redis():
            meta_key = f"{self._session_key(session_id)}:meta"
            await self._redis.setex(meta_key, ttl, json.dumps({
                "created_at": datetime.utcnow().isoformat(),
                "session_id": session_id,
            }, ensure_ascii=False))
            logger.info(f"创建 Redis 对话会话: {session_id}")
        else:
            if session_id not in self._fallback:
                self._fallback[session_id] = []
            logger.warning(f"Redis 不可用，降级为内存存储: {session_id}")

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        ttl: int = 86400 * 7,
    ) -> None:
        """添加消息到对话历史

        Args:
            session_id: 会话ID
            role: 角色（"user" 或 "assistant"）
            content: 消息内容
            metadata: 元数据
            ttl: 消息过期时间（秒）
        """
        msg = ConversationMessage(
            role=role,
            content=content,
            timestamp=datetime.utcnow(),
            metadata=metadata or {},
        )
        msg_dict = {
            "role": msg.role,
            "content": msg.content,
            "timestamp": msg.timestamp.isoformat(),
            "metadata": msg.metadata,
        }

        if await self._ensure_redis():
            key = self._session_key(session_id)
            pipe = self._redis.pipeline()
            pipe.rpush(key, json.dumps(msg_dict, ensure_ascii=False))
            pipe.expire(key, ttl)
            pipe.ltrim(key, -self.max_history_length, -1)
            await pipe.execute()
            logger.debug(f"添加消息到 Redis 会话 {session_id}")
        else:
            if session_id not in self._fallback:
                self._fallback[session_id] = []
            self._fallback[session_id].append(msg)
            if len(self._fallback[session_id]) > self.max_history_length:
                self._fallback[session_id] = self._fallback[session_id][-self.max_history_length:]

    async def get_recent_messages(
        self, session_id: str, count: int = 10
    ) -> List[ConversationMessage]:
        """获取最近的对话消息

        Args:
            session_id: 会话ID
            count: 消息数量

        Returns:
            最近的对话消息列表
        """
        if await self._ensure_redis():
            key = self._session_key(session_id)
            raw_messages = await self._redis.lrange(key, -count, -1)
            return [
                ConversationMessage(
                    role=m["role"],
                    content=m["content"],
                    timestamp=datetime.fromisoformat(m["timestamp"]),
                    metadata=m.get("metadata", {}),
                )
                for raw in raw_messages
                for m in [json.loads(raw)]
            ]
        else:
            msgs = self._fallback.get(session_id, [])
            return msgs[-count:]

    def get_conversation_history(self, session_id: str) -> List[Dict[str, Any]]:
        """获取对话历史（同步方法，用于向后兼容）

        Args:
            session_id: 会话ID

        Returns:
            对话历史列表
        """
        if session_id not in self._fallback:
            return []

        return [
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": msg.timestamp.isoformat(),
                "metadata": msg.metadata,
            }
            for msg in self._fallback[session_id]
        ]

    async def get_conversation_context(
        self, session_id: str, max_length: int = 3000
    ) -> str:
        """获取格式化的对话上下文（用于Agent提示）

        Args:
            session_id: 会话ID
            max_length: 最大字符长度

        Returns:
            格式化的对话上下文
        """
        messages = await self.get_recent_messages(session_id)
        if not messages:
            return ""

        parts = []
        current_len = 0
        for msg in reversed(messages):
            line = f"{msg.role}: {msg.content}"
            if current_len + len(line) > max_length:
                break
            parts.insert(0, line)
            current_len += len(line)
        return "\n".join(parts)

    async def clear_session(self, session_id: str) -> None:
        """清空指定会话的历史

        Args:
            session_id: 会话ID
        """
        if await self._ensure_redis():
            keys_to_delete = [
                self._session_key(session_id),
                f"{self._session_key(session_id)}:meta",
            ]
            await self._redis.delete(*keys_to_delete)
        self._fallback.pop(session_id, None)
        logger.info(f"清空会话: {session_id}")

    async def session_exists(self, session_id: str) -> bool:
        """检查会话是否存在

        Args:
            session_id: 会话ID

        Returns:
            是否存在
        """
        if await self._ensure_redis():
            return await self._redis.exists(self._session_key(session_id)) > 0
        return session_id in self._fallback

    def get_session_ids(self) -> List[str]:
        """获取所有活跃的会话ID（同步方法，向后兼容）

        Returns:
            会话ID列表
        """
        return list(self._fallback.keys())

    async def list_user_conversations(
        self, user_id: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """列出用户的所有会话

        Args:
            user_id: 用户ID
            limit: 返回的最大会话数

        Returns:
            会话列表，按创建时间倒序
        """
        if await self._ensure_redis():
            pattern = f"{self.CHAT_PREFIX}{user_id}:*"
            keys = []
            cursor = 0
            while True:
                cursor, batch = await self._redis.scan(
                    cursor, match=pattern, count=100
                )
                keys.extend(b for b in batch if not b.endswith(b":meta"))
                if cursor == 0:
                    break

            conversations = []
            for key in keys[:limit]:
                msg_count = await self._redis.llen(key)
                session_id = key.decode().split(":")[-1]
                meta_raw = await self._redis.get(f"{key.decode()}:meta")
                meta = json.loads(meta_raw) if meta_raw else {}
                conversations.append({
                    "session_id": session_id,
                    "message_count": msg_count,
                    "created_at": meta.get("created_at"),
                })

            return sorted(
                conversations,
                key=lambda x: x.get("created_at", ""),
                reverse=True,
            )
        return [
            {"session_id": sid, "message_count": len(msgs)}
            for sid, msgs in self._fallback.items()
            if sid.startswith(user_id)
        ]

    def get_session_stats(self, session_id: str) -> Dict[str, Any]:
        """获取会话统计信息（同步方法，向后兼容）

        Args:
            session_id: 会话ID

        Returns:
            统计信息字典
        """
        if session_id not in self._fallback:
            return {"error": "Session not found"}

        messages = self._fallback[session_id]
        user_messages = [m for m in messages if m.role == "user"]
        assistant_messages = [m for m in messages if m.role == "assistant"]

        return {
            "total_messages": len(messages),
            "user_messages": len(user_messages),
            "assistant_messages": len(assistant_messages),
            "last_message_time": (
                messages[-1].timestamp.isoformat() if messages else None
            ),
            "session_created": (
                messages[0].timestamp.isoformat() if messages else None
            ),
        }


# 全局记忆管理器实例（延迟初始化 Redis）
conversation_memory = ConversationMemory()