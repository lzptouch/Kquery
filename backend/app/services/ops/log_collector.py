"""运维日志采集与聚合能力。

- 应用内日志统一采集（来源 / 级别 / 消息 / 元数据）
- 可选对接 Elasticsearch 做集中式日志聚合（OPS_ELASTICSEARCH_ENABLED=true）
- 查询 / 统计 / 关键字检索（ES 不可用时降级为本地 Redis/内存存储）

无论是否对接 ES，采集与查询接口均可用。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app.core.settings import get_settings
from app.services.ops._store import StateStore

logger = logging.getLogger(__name__)

VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class LogCollector:
    """日志采集器（单例）。"""

    _instance: Optional["LogCollector"] = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self.store = StateStore(namespace="kquery:ops:logs")
        self._es = None
        self._es_ready = False

    @classmethod
    def instance(cls) -> "LogCollector":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- 采集 ----------

    async def collect(
        self,
        source: str,
        level: str,
        message: str,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ) -> Dict[str, Any]:
        level = (level or "INFO").upper()
        if level not in VALID_LEVELS:
            level = "INFO"
        record = {
            "id": f"log_{uuid.uuid4().hex[:12]}",
            "source": source,
            "level": level,
            "message": message,
            "metadata": metadata or {},
            "timestamp": timestamp or time.time(),
        }
        await self.store.push_list("all", record, max_len=5000)
        await self._maybe_push_es(record)
        return record

    async def _maybe_push_es(self, record: Dict[str, Any]) -> None:
        if not self.settings.ops.elasticsearch_enabled:
            return
        try:
            from elasticsearch import AsyncElasticsearch

            if not self._es_ready:
                self._es = AsyncElasticsearch(
                    self.settings.ops.elasticsearch_url, verify_certs=False
                )
                self._es_ready = True
            doc = dict(record)
            doc["@timestamp"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(record["timestamp"])
            )
            index = self.settings.ops.elasticsearch_index
            await self._es.index(index=index, document=doc)
        except ModuleNotFoundError:
            logger.warning("未安装 elasticsearch 客户端，日志仅保存在本地存储")
        except Exception as exc:  # pragma: no cover - ES 连接异常
            logger.warning("推送 Elasticsearch 失败（降级本地存储）: %s", exc)

    # ---------- 查询 ----------

    async def query(
        self,
        source: Optional[str] = None,
        level: Optional[str] = None,
        since: Optional[float] = None,
        keyword: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        logs = await self.store.get_list("all", 0, limit - 1)
        result: List[Dict[str, Any]] = []
        for log in logs:
            if source and log.get("source") != source:
                continue
            if level and log.get("level") != level.upper():
                continue
            if since and log.get("timestamp", 0) < since:
                continue
            if keyword and keyword.lower() not in log.get("message", "").lower():
                continue
            result.append(log)
        result.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return result[:limit]

    async def tail(self, n: int = 50) -> List[Dict[str, Any]]:
        return await self.query(limit=n)

    async def stats(self) -> Dict[str, Any]:
        logs = await self.store.get_list("all", 0, 1999)
        by_level: Dict[str, int] = {}
        by_source: Dict[str, int] = {}
        for log in logs:
            by_level[log.get("level", "UNKNOWN")] = by_level.get(log.get("level", "UNKNOWN"), 0) + 1
            by_source[log.get("source", "unknown")] = by_source.get(log.get("source", "unknown"), 0) + 1
        return {
            "total_in_window": len(logs),
            "by_level": by_level,
            "by_source": by_source,
            "elasticsearch_enabled": self.settings.ops.elasticsearch_enabled,
        }
