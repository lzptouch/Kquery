"""运维健康巡检能力。

对系统关键组件进行健康探测并量化延迟：
- redis / neo4j / milvus / mongodb 外部依赖
- api 应用进程
- disk 磁盘空间 / memory 内存占用

探测结果实时写入 MetricsCollector 的 service_health 指标，并保留最近历史。
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from typing import Any, Dict, List, Optional

from app.core.cache import redis_cache_service
from app.core.connection_pool import (
    MilvusConnectionManager,
    Neo4jConnectionManager,
)
from app.core.settings import get_settings
from app.services.ops._store import StateStore
from app.services.ops.metrics_collector import MetricsCollector

logger = logging.getLogger(__name__)

# 所有可巡检组件
COMPONENTS = ["redis", "neo4j", "milvus", "mongodb", "api", "disk", "memory"]


class HealthChecker:
    """健康巡检器（单例）。"""

    _instance: Optional["HealthChecker"] = None

    def __init__(self) -> None:
        self.settings = get_settings()
        self.store = StateStore(namespace="kquery:ops:health")
        self.metrics = MetricsCollector.instance()

    @classmethod
    def instance(cls) -> "HealthChecker":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- 各组件探测 ----------

    async def _check_redis(self) -> Dict[str, Any]:
        r = redis_cache_service.redis
        if r is None:
            return {"status": "down", "detail": "Redis 客户端未初始化"}
        t0 = time.time()
        await r.ping()
        return {"status": "up", "latency_ms": round((time.time() - t0) * 1000, 2)}

    async def _check_neo4j(self) -> Dict[str, Any]:
        def _sync() -> Dict[str, Any]:
            driver = Neo4jConnectionManager.get_driver()
            if driver is None:
                return {"status": "down", "detail": "Neo4j 驱动不可用"}
            t0 = time.time()
            with driver.session() as s:
                s.run("RETURN 1").single()
            return {"status": "up", "latency_ms": round((time.time() - t0) * 1000, 2)}

        try:
            return await asyncio.to_thread(_sync)
        except Exception as exc:
            return {"status": "down", "detail": str(exc)[:200]}

    async def _check_milvus(self) -> Dict[str, Any]:
        try:
            ok = await asyncio.to_thread(MilvusConnectionManager.check_connection)
            return {"status": "up" if ok else "down", "detail": "" if ok else "未连接"}
        except Exception as exc:
            return {"status": "down", "detail": str(exc)[:200]}

    async def _check_mongodb(self) -> Dict[str, Any]:
        url = self.settings.mongodb_url
        if not url:
            return {"status": "unknown", "detail": "未配置 MongoDB"}
        # 优先使用 motor 异步客户端
        try:
            from motor.motor_asyncio import AsyncIOMotorClient

            t0 = time.time()
            client = AsyncIOMotorClient(url, serverSelectionTimeoutMS=3000)
            await client.admin.command("ping")
            return {"status": "up", "latency_ms": round((time.time() - t0) * 1000, 2)}
        except ModuleNotFoundError:
            return await self._check_tcp(url, default_port=27017)
        except Exception as exc:
            return {"status": "down", "detail": str(exc)[:200]}

    @staticmethod
    async def _check_tcp(url: str, default_port: int) -> Dict[str, Any]:
        # 解析 mongodb://user:pass@host:port/db
        try:
            import re

            m = re.search(r"@([^/]+)", url)
            hostport = m.group(1) if m else url.split("://")[-1].split("/")[0]
            if ":" in hostport:
                host, port = hostport.split(":")
                port = int(port)
            else:
                host, port = hostport, default_port
            t0 = time.time()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=3
            )
            writer.close()
            await writer.wait_closed()
            return {"status": "up", "latency_ms": round((time.time() - t0) * 1000, 2)}
        except Exception as exc:
            return {"status": "down", "detail": str(exc)[:200]}

    async def _check_api(self) -> Dict[str, Any]:
        return {"status": "up", "detail": "应用进程存活"}

    async def _check_disk(self) -> Dict[str, Any]:
        try:
            import shutil

            total, used, free = shutil.disk_usage(os.getcwd())
            used_pct = round(used / total * 100, 2)
            status = "up" if used_pct < 90 else "warning"
            return {
                "status": status,
                "detail": f"使用率 {used_pct}%",
                "used_percent": used_pct,
                "free_gb": round(free / (1024 ** 3), 2),
            }
        except Exception as exc:
            return {"status": "unknown", "detail": str(exc)[:200]}

    async def _check_memory(self) -> Dict[str, Any]:
        try:
            import psutil

            vm = psutil.virtual_memory()
            used_pct = round(vm.percent, 2)
            status = "up" if used_pct < 90 else "warning"
            return {
                "status": status,
                "detail": f"使用率 {used_pct}%",
                "used_percent": used_pct,
                "available_gb": round(vm.available / (1024 ** 3), 2),
            }
        except ModuleNotFoundError:
            return {"status": "unknown", "detail": "未安装 psutil，跳过内存检查"}
        except Exception as exc:
            return {"status": "unknown", "detail": str(exc)[:200]}

    _CHECKERS = {
        "redis": "_check_redis",
        "neo4j": "_check_neo4j",
        "milvus": "_check_milvus",
        "mongodb": "_check_mongodb",
        "api": "_check_api",
        "disk": "_check_disk",
        "memory": "_check_memory",
    }

    # ---------- 对外接口 ----------

    async def run_check(self, component: str) -> Dict[str, Any]:
        if component not in self._CHECKERS:
            raise ValueError(f"未知组件: {component}，可选: {COMPONENTS}")
        checker = getattr(self, self._CHECKERS[component])
        t0 = time.time()
        try:
            result = await checker()
        except Exception as exc:
            result = {"status": "down", "detail": str(exc)[:200]}
        result["component"] = component
        result["checked_at"] = time.time()
        result["elapsed_ms"] = round((time.time() - t0) * 1000, 2)
        # 更新指标
        self.metrics.set_service_health(component, result.get("status") == "up")
        # 保留最近 50 条历史
        await self.store.push_list(f"history:{component}", result, max_len=50)
        return result

    async def run_all_checks(self) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        for comp in COMPONENTS:
            results[comp] = await self.run_check(comp)
        summary = {
            "checked_at": time.time(),
            "components": results,
            "healthy": sum(1 for r in results.values() if r.get("status") == "up"),
            "total": len(results),
            "all_up": all(r.get("status") == "up" for r in results.values()),
        }
        await self.store.set_json("last_summary", summary)
        return summary

    async def get_last_summary(self) -> Optional[Dict[str, Any]]:
        return await self.store.get_json("last_summary")

    async def get_history(self, component: str, limit: int = 20) -> List[Dict[str, Any]]:
        return await self.store.get_list(f"history:{component}", 0, limit - 1)
