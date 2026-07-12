"""WebSocket连接管理器。

管理WebSocket连接的生命周期，支持连接注册、消息广播和心跳检测。
归属于L1多端交互层。

使用示例:
    from app.api.ws_manager import ws_manager

    # 在WebSocket端点中
    await ws_manager.connect(websocket, session_id)
    try:
        while True:
            data = await ws_manager.receive(websocket)
            response = await process_message(data)
            await ws_manager.send(websocket, response)
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, session_id)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect

from app.core.logger import get_logger

logger = get_logger(__name__)


class WebSocketManager:
    """WebSocket连接管理器。

    支持多会话并发，每个会话可关联多个连接。

    Attributes:
        _connections: session_id -> set of WebSocket connections。
    """

    def __init__(self) -> None:
        self._connections: Dict[str, Set[WebSocket]] = {}
        self._active_connections: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, session_id: str) -> None:
        """接受WebSocket连接并注册。

        Args:
            websocket: WebSocket连接实例。
            session_id: 关联的会话ID。
        """
        await websocket.accept()
        if session_id not in self._connections:
            self._connections[session_id] = set()
        self._connections[session_id].add(websocket)
        self._active_connections[websocket] = session_id
        logger.info(
            f"WebSocket连接已建立: session_id={session_id}, "
            f"active_connections={len(self._active_connections)}"
        )

    def disconnect(self, websocket: WebSocket, session_id: str) -> None:
        """断开WebSocket连接并清理。

        Args:
            websocket: WebSocket连接实例。
            session_id: 关联的会话ID。
        """
        if session_id in self._connections:
            self._connections[session_id].discard(websocket)
            if not self._connections[session_id]:
                del self._connections[session_id]

        self._active_connections.pop(websocket, None)
        logger.debug(
            f"WebSocket已断开: session_id={session_id}, "
            f"remaining={self._active_connections_count(session_id)}"
        )

    async def send(self, websocket: WebSocket, data: Dict[str, Any]) -> None:
        """向指定连接发送JSON消息。

        Args:
            websocket: WebSocket连接实例。
            data: 消息数据字典。
        """
        try:
            await websocket.send_json(data)
        except WebSocketDisconnect:
            session_id = self._active_connections.get(websocket, "unknown")
            self.disconnect(websocket, session_id)
        except Exception as e:
            logger.error(f"WebSocket发送失败: {e}")

    async def send_to_session(
        self,
        session_id: str,
        data: Dict[str, Any],
    ) -> None:
        """向会话的所有连接广播消息。

        Args:
            session_id: 目标会话ID。
            data: 消息数据字典。
        """
        connections = self._connections.get(session_id, set()).copy()
        for ws in connections:
            await self.send(ws, data)

    async def receive(self, websocket: WebSocket) -> Dict[str, Any]:
        """接收WebSocket消息。

        Args:
            websocket: WebSocket连接实例。

        Returns:
            解析后的消息字典。

        Raises:
            WebSocketDisconnect: 连接已断开。
        """
        try:
            data = await websocket.receive_json()
            return data
        except WebSocketDisconnect:
            session_id = self._active_connections.get(websocket, "unknown")
            self.disconnect(websocket, session_id)
            raise

    async def broadcast(self, data: Dict[str, Any]) -> None:
        """向所有活跃连接广播消息。

        Args:
            data: 消息数据字典。
        """
        for session_id in list(self._connections.keys()):
            await self.send_to_session(session_id, data)

    def get_active_session_ids(self) -> list[str]:
        """获取所有活跃会话ID列表。

        Returns:
            活跃会话ID列表。
        """
        return list(self._connections.keys())

    def get_connection_count(self) -> int:
        """获取活跃连接数。

        Returns:
            活跃连接数。
        """
        return len(self._active_connections)

    def _active_connections_count(self, session_id: str) -> int:
        """获取指定会话的连接数。

        Args:
            session_id: 会话ID。

        Returns:
            连接数。
        """
        return len(self._connections.get(session_id, set()))


# 全局单例
ws_manager = WebSocketManager()
