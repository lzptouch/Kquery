"""语义级缓存（阶段 2 README §2.2 / §3.5）。

- 缓存键 = 摘要（租户 + 问题原文 + 模型版本标识 + 检索与生成配置摘要），
  三者任一变化即换键——换模型后旧条目自然失效，无需手动清缓存；
- 缓存值是**完整响应结构**（答案 + 证据 + 检索统计），命中时前端拿到
  的数据与新鲜请求同构；
- TTL 1 小时自动过期，不做手动清理；
- 防两种误判：键里不混入时间戳/请求 ID（假失效），不漏掉租户/模型版本
  （假命中→跨租户串答案属数据越权）。

生产环境应替换为键值存储（Redis），运行期数据与业务数据分卷存放。
"""
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol


def make_cache_key(tenant_id: str, question: str, model_version: str, config_digest: str) -> str:
    """缓存键：租户 + 问题 + 模型版本 + 配置摘要 的联合摘要。"""
    raw = json.dumps(
        {
            "tenant": tenant_id,
            "question": question,
            "model": model_version,
            "config": config_digest,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def config_digest(payload: dict) -> str:
    """检索与生成配置摘要（配置变了键就变）。"""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class CacheEntry:
    payload: dict  # 完整响应体副本
    created_at: float
    ttl_seconds: float
    hit_count: int = 0
    last_hit_at: float = field(default=0.0)


class SemanticCache(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...

    def set(self, key: str, payload: dict, ttl_seconds: float) -> None: ...

    def stats(self) -> dict: ...


class InMemorySemanticCache:
    """进程内存实现（开发/单实例）；生产换 Redis 并保持同一接口。"""

    def __init__(self, max_entries: int = 10000):
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if time.time() - entry.created_at > entry.ttl_seconds:
                self._entries.pop(key, None)
                return None
            entry.hit_count += 1
            entry.last_hit_at = time.time()
            return entry

    def set(self, key: str, payload: dict, ttl_seconds: float) -> None:
        with self._lock:
            if len(self._entries) >= self._max_entries:
                # 简单逐出：删最早创建且命中最少的一批
                victims = sorted(
                    self._entries.items(), key=lambda kv: (kv[1].hit_count, kv[1].created_at)
                )[: self._max_entries // 10]
                for k, _ in victims:
                    self._entries.pop(k, None)
            self._entries[key] = CacheEntry(
                payload=payload, created_at=time.time(), ttl_seconds=ttl_seconds
            )

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            alive = [e for e in self._entries.values() if now - e.created_at <= e.ttl_seconds]
            return {
                "entries": len(alive),
                "total_hits": sum(e.hit_count for e in alive),
            }


def build_cache(provider: str = "memory") -> SemanticCache:
    # 预留：生产接 Redis 时在此按 provider 分支
    return InMemorySemanticCache()
