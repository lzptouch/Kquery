"""语义级缓存。

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
    # 语义近似检索用：问题向量 + 规范化后的问题文本
    vector: list = field(default_factory=list)
    question: str = ""
    # 租户归属：近似命中必须按它过滤，否则跨租户串答案
    tenant_id: str = ""
    # 维度签名（KQ-04）：模型版本 + 配置摘要（含 user/acl_version/index_version）。
    # 近似命中必须签名完全一致——只按租户过滤会把 A 用户的答案命中给同租户 B 用户。
    signature: str = ""


class SemanticCache(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...

    def set(self, key: str, payload: dict, ttl_seconds: float) -> None: ...

    def stats(self) -> dict: ...


class InMemorySemanticCache:
    """进程内存实现（开发/单实例）；生产换 Redis 并保持同一接口。

    真正的**语义级**缓存：除了按键精确命中，还支持"问题向量余弦 ≥ 阈值"的近似命中，
    这样"保修政策 vs 保修条款"这类改写也能省下一次模型调用。文档把它写成
    "语义级缓存 / 命中率 ≥30%"，只做 SHA256 精确哈希是达不到的。
    """

    def __init__(
        self,
        max_entries: int = 10000,
        embeddings=None,
        sim_threshold: float = 0.95,
        max_scan: int = 2000,
    ):
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._embeddings = embeddings
        self._threshold = sim_threshold
        self._max_scan = max_scan

    # ---- 过期清理 ----

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

    def set(
        self,
        key: str,
        payload: dict,
        ttl_seconds: float,
        question: str = "",
        vector: list | None = None,
        tenant_id: str = "",
        signature: str = "",
    ) -> None:
        with self._lock:
            if len(self._entries) >= self._max_entries:
                # 简单逐出：删最早创建且命中最少的一批
                victims = sorted(
                    self._entries.items(), key=lambda kv: (kv[1].hit_count, kv[1].created_at)
                )[: max(1, self._max_entries // 10)]
                for k, _ in victims:
                    self._entries.pop(k, None)
            self._entries[key] = CacheEntry(
                payload=payload,
                created_at=time.time(),
                ttl_seconds=ttl_seconds,
                vector=list(vector or []),
                question=question or str(payload.get("question", "")),
                tenant_id=tenant_id,
                signature=signature,
            )

    def lookup(
        self,
        question: str,
        exact_key: str,
        vector: list | None = None,
        tenant_id: str = "",
        signature: str = "",
    ) -> tuple[CacheEntry | None, str]:
        """语义级查找：先按键精确命中，再退回向量近似命中。

        返回 (entry, hit_kind)，hit_kind ∈ {"exact", "semantic", "miss"}。
        近似命中的两个硬条件（KQ-04）：
        - 同一租户（否则跨租户串答案，）；
        - 维度签名完全一致（模型版本 + user + acl_version + index_version + 配置摘要），
          否则同租户内 A 用户的答案会命中给 B 用户，权限/配置变更后还会命中旧条目。
        """
        exact = self.get(exact_key)
        if exact is not None:
            return exact, "exact"
        # 只要求调用方给得出问题向量：是否持有编码器是调用侧的职责，
        # 缓存自己不该把"没装编码器"等同于"不能近似命中"
        if not vector or not signature:
            return None, "miss"
        now = time.time()
        best: CacheEntry | None = None
        best_sim = self._threshold
        with self._lock:
            scanned = 0
            for entry in self._entries.values():
                # 租户隔离第一闸：近似命中只认同租户条目（兑现上方注释承诺）
                if entry.tenant_id != tenant_id:
                    continue
                # 维度签名第二闸：主体/权限/数据版本/模型/配置任一不同即不可命中
                if entry.signature != signature:
                    continue
                if now - entry.created_at > entry.ttl_seconds:
                    continue
                if not entry.vector:
                    continue
                scanned += 1
                if scanned > self._max_scan:
                    break
                sim = _cosine(vector, entry.vector)
                if sim >= best_sim:
                    best_sim, best = sim, entry
        if best is None:
            return None, "miss"
        best.hit_count += 1
        best.last_hit_at = now
        return best, "semantic"

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            alive = [e for e in self._entries.values() if now - e.created_at <= e.ttl_seconds]
            return {
                "entries": len(alive),
                "total_hits": sum(e.hit_count for e in alive),
                "semantic_ready": self._embeddings is not None,
                "sim_threshold": self._threshold,
            }


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def build_cache(
    provider: str = "memory",
    settings=None,
    embeddings=None,
    tenant_id: str = "",
) -> SemanticCache:
    # 预留：生产接 Redis 时在此按 provider 分支
    return InMemorySemanticCache(
        embeddings=embeddings,
        sim_threshold=getattr(settings, "cache_sim_threshold", 0.95) if settings else 0.95,
        max_scan=getattr(settings, "cache_max_scan", 2000) if settings else 2000,
    )
