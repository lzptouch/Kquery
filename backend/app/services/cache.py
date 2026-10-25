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


def make_cache_key(
    tenant_id: str, question: str, model_version: str, config_digest: str, scope: str = ""
) -> str:
    """缓存键：租户 + 问题 + 模型版本 + 配置摘要（+ 作用域）的联合摘要。

    `scope` 是 F4 快问引入的**检索范围维度**：快问只检索某一个临时文档，
    与全库检索的问题文本可能完全相同（"这个文件讲了什么"）。
    少了这个维度，两种范围的答案会互相命中——用户拿到的是**另一个范围**的答案，
    属于正确性事故（跨文档串答案）。
    """
    raw = json.dumps(
        {
            "tenant": tenant_id,
            "question": question,
            "model": model_version,
            "config": config_digest,
            "scope": scope,
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
    # 租户归属（DEF-R-22）：近似命中必须按它过滤，否则跨租户串答案
    tenant_id: str = ""
    # 检索范围（F4 快问）：doc_id 维度。近似命中必须按它过滤，
    # 否则"针对 A 文档的答案"会被"针对 B 文档的相似问题"命中——同样是串答案。
    scope: str = ""


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
        scope: str = "",
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
                scope=scope,
            )

    def lookup(
        self,
        question: str,
        exact_key: str,
        vector: list | None = None,
        tenant_id: str = "",
        scope: str = "",
    ) -> tuple[CacheEntry | None, str]:
        """语义级查找：先按键精确命中，再退回向量近似命中。

        返回 (entry, hit_kind)，hit_kind ∈ {"exact", "semantic", "miss"}。
        近似命中会把候选按余弦从高到低扫描一遍，只认**同一租户**的条目——
        此前实现遍历全部 entries 并未按租户过滤（与下方注释自相矛盾），
        导致 A 租户的答案可被 B 租户近似命中（DEF-R-22 跨租户串答案）。

        `scope` 是第二道隔离闸（F4 快问）：检索范围不同的条目一律不互相命中。
        """
        exact = self.get(exact_key)
        if exact is not None:
            return exact, "exact"
        # 只要求调用方给得出问题向量：是否持有编码器是调用侧的职责，
        # 缓存自己不该把"没装编码器"等同于"不能近似命中"
        if not vector:
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
                # 范围隔离第二闸：快问（doc_id）与全库检索的答案不得互相命中
                if entry.scope != scope:
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



class RedisSemanticCache:
    """共享存储实现（多副本共享同一份缓存）。

    ## 两种命中的边界（说清楚比含糊更重要）

    - **精确命中**：跨副本。条目连同 payload 一起存在共享存储里，
      A 副本写入的，B 副本能直接命中——这才是"多副本共享"的意义。
    - **近似（语义）命中**：**本副本内**。共享存储里存了向量，但按余弦
      全表扫描在 Redis 上不可行（会把 O(N) 扫描变成网络往返）。
      因此近似索引留在本进程：跨副本能省下**完全相同**的问题，
      语义相近的改写仍需各副本自己命中。

    ## 降级

    共享存储不可用时，`SharedState` 门面会自动回落到进程内实现，
    请求照常返回（Fail-Open）。缓存不可用只该表现为"命中率低"，
    绝不该表现为"请求失败"。
    """

    KEY_PREFIX = "semcache:"

    def __init__(
        self,
        state,
        embeddings=None,
        sim_threshold: float = 0.95,
        max_scan: int = 2000,
        max_entries: int = 10000,
    ):
        self._state = state
        self._embeddings = embeddings
        self._threshold = sim_threshold
        self._max_scan = max_scan
        self._max_entries = max_entries
        # 本地近似索引：key → (vector, tenant_id, scope, created_at, ttl)
        self._index: dict[str, tuple[list, str, str, float, float]] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    def _k(self, key: str) -> str:
        return f"{self.KEY_PREFIX}{key}"

    # ---- 序列化 ----

    @staticmethod
    def _encode(entry: CacheEntry) -> str:
        return json.dumps(
            {
                "payload": entry.payload,
                "created_at": entry.created_at,
                "ttl_seconds": entry.ttl_seconds,
                "question": entry.question,
                "tenant_id": entry.tenant_id,
                "scope": entry.scope,
            },
            ensure_ascii=False,
            default=str,
        )

    @staticmethod
    def _decode(raw: str) -> CacheEntry | None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return CacheEntry(
            payload=data.get("payload") or {},
            created_at=float(data.get("created_at") or 0.0),
            ttl_seconds=float(data.get("ttl_seconds") or 0.0),
            question=data.get("question", ""),
            tenant_id=data.get("tenant_id", ""),
            scope=data.get("scope", ""),
        )

    # ---- SemanticCache 接口 ----

    def get(self, key: str) -> CacheEntry | None:
        try:
            raw = self._state.get(self._k(key))
        except Exception:  # noqa: BLE001 - 存储异常 → 视作未命中
            return None
        if not raw:
            return None
        entry = self._decode(raw)
        if entry is None:
            return None
        if time.time() - entry.created_at > entry.ttl_seconds:
            return None
        entry.hit_count += 1
        entry.last_hit_at = time.time()
        with self._lock:
            self._hits += 1
        return entry

    def set(
        self,
        key: str,
        payload: dict,
        ttl_seconds: float,
        question: str = "",
        vector: list | None = None,
        tenant_id: str = "",
        scope: str = "",
    ) -> None:
        entry = CacheEntry(
            payload=payload,
            created_at=time.time(),
            ttl_seconds=ttl_seconds,
            question=question or str(payload.get("question", "")),
            tenant_id=tenant_id,
            scope=scope,
        )
        try:
            self._state.set(self._k(key), self._encode(entry), int(ttl_seconds) or 0)
        except Exception:  # noqa: BLE001 - 写失败不影响本次返回
            pass
        if vector:
            with self._lock:
                if len(self._index) >= self._max_entries:
                    # 简单逐出：丢最早写入的
                    oldest = min(self._index.items(), key=lambda kv: kv[1][3])[0]
                    self._index.pop(oldest, None)
                self._index[key] = (list(vector), tenant_id, scope, entry.created_at, ttl_seconds)

    def lookup(
        self,
        question: str,
        exact_key: str,
        vector: list | None = None,
        tenant_id: str = "",
        scope: str = "",
    ) -> tuple[CacheEntry | None, str]:
        exact = self.get(exact_key)
        if exact is not None:
            return exact, "exact"
        if not vector:
            with self._lock:
                self._misses += 1
            return None, "miss"
        now = time.time()
        best_key: str | None = None
        best_sim = self._threshold
        with self._lock:
            scanned = 0
            for key, (vec, entry_tenant, entry_scope, created_at, ttl) in self._index.items():
                if entry_tenant != tenant_id:
                    continue
                if entry_scope != scope:
                    continue
                if now - created_at > ttl:
                    continue
                scanned += 1
                if scanned > self._max_scan:
                    break
                sim = _cosine(vector, vec)
                if sim >= best_sim:
                    best_sim, best_key = sim, key
        if best_key is None:
            with self._lock:
                self._misses += 1
            return None, "miss"
        entry = self.get(best_key)
        if entry is None:
            with self._lock:
                self._misses += 1
            return None, "miss"
        return entry, "semantic"

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._index),
                "total_hits": self._hits,
                "semantic_ready": self._embeddings is not None,
                "sim_threshold": self._threshold,
                "backend": "redis",
            }


def build_cache(
    provider: str = "memory",
    settings=None,
    embeddings=None,
    tenant_id: str = "",
    state=None,
) -> SemanticCache:
    """语义缓存工厂：按后端选择实现。

    `provider`：
    - `memory`：进程内（单副本 / 开发）；
    - `redis`：共享存储（多副本）；
    - `auto`：有 Redis 连接就用 Redis，否则内存。

    硬约束：**Redis 不可用必须降级为内存**，绝不把"Redis 挂了"变成"请求失败"。
    """
    backend = (provider or "memory").lower()
    if backend == "auto":
        backend = "redis" if (state is not None and getattr(state, "redis_enabled", False)) else "memory"
    if backend == "redis" and state is not None:
        return RedisSemanticCache(
            state,
            embeddings=embeddings,
            sim_threshold=getattr(settings, "cache_sim_threshold", 0.95) if settings else 0.95,
            max_scan=getattr(settings, "cache_max_scan", 2000) if settings else 2000,
            max_entries=getattr(settings, "cache_max_entries", 10000) if settings else 10000,
        )
    return InMemorySemanticCache(
        embeddings=embeddings,
        sim_threshold=getattr(settings, "cache_sim_threshold", 0.95) if settings else 0.95,
        max_scan=getattr(settings, "cache_max_scan", 2000) if settings else 2000,
    )
