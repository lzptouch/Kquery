"""运维模块共享的状态存储工具。

基于 Redis 的轻量键值/列表存储。当 Redis 不可用（例如仅启动了部分
基础设施）时，自动降级为进程内内存存储，保证运维 API 始终可用。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from app.core.cache import redis_cache_service

logger = logging.getLogger(__name__)


class StateStore:
    """简单的 Redis 键值/列表存储，带内存降级。"""

    def __init__(self, namespace: str = "kquery:ops"):
        self.ns = namespace
        self._mem: Dict[str, Any] = {}

    def _key(self, key: str) -> str:
        return f"{self.ns}:{key}"

    async def set_json(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        payload = json.dumps(value, default=str, ensure_ascii=False)
        try:
            r = redis_cache_service.redis
            if r is not None:
                await r.set(self._key(key), payload, ex=ttl)
                return
        except Exception as exc:  # pragma: no cover - 网络/连接异常
            logger.warning("StateStore.set_json Redis 失败，降级内存: %s", exc)
        self._mem[self._key(key)] = payload

    async def get_json(self, key: str, default: Any = None) -> Any:
        try:
            r = redis_cache_service.redis
            if r is not None:
                raw = await r.get(self._key(key))
                if raw is not None:
                    return json.loads(raw)
        except Exception as exc:  # pragma: no cover
            logger.warning("StateStore.get_json Redis 失败: %s", exc)
        raw = self._mem.get(self._key(key))
        return json.loads(raw) if raw is not None else default

    async def delete(self, key: str) -> None:
        try:
            r = redis_cache_service.redis
            if r is not None:
                await r.delete(self._key(key))
        except Exception:
            pass
        self._mem.pop(self._key(key), None)

    async def push_list(self, key: str, value: Any, max_len: int = 1000) -> None:
        payload = json.dumps(value, default=str, ensure_ascii=False)
        try:
            r = redis_cache_service.redis
            if r is not None:
                full = self._key(key)
                await r.lpush(full, payload)
                await r.ltrim(full, 0, max_len - 1)
                return
        except Exception:  # pragma: no cover
            pass
        lst = self._mem.setdefault(self._key(key), [])
        lst.insert(0, payload)
        self._mem[self._key(key)] = lst[:max_len]

    async def get_list(self, key: str, start: int = 0, end: int = -1) -> List[Any]:
        try:
            r = redis_cache_service.redis
            if r is not None:
                items = await r.lrange(self._key(key), start, end)
                return [json.loads(i) for i in items]
        except Exception:  # pragma: no cover
            pass
        lst = self._mem.get(self._key(key), [])
        if end == -1:
            sliced = lst[start:]
        else:
            sliced = lst[start : end + 1]
        return [json.loads(i) for i in sliced]

    async def list_len(self, key: str) -> int:
        try:
            r = redis_cache_service.redis
            if r is not None:
                return await r.llen(self._key(key))
        except Exception:  # pragma: no cover
            pass
        return len(self._mem.get(self._key(key), []))
