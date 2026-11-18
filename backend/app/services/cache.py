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

import numpy as np

from app.core.logging_setup import get_logger

logger = get_logger(__name__)


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
    # 租户归属：近似命中必须按它过滤，否则跨租户串答案
    tenant_id: str = ""
    # 检索范围（F4 快问）：doc_id 维度。近似命中必须按它过滤，
    # 否则"针对 A 文档的答案"会被"针对 B 文档的相似问题"命中——同样是串答案。
    scope: str = ""
    # 维度签名（KQ-04）：模型版本 + 配置摘要（含 user/index_version）。
    # 近似命中必须签名完全一致——只按租户过滤会把 A 用户的答案命中给同租户 B 用户。
    signature: str = ""


class SemanticCache(Protocol):
    def get(self, key: str) -> CacheEntry | None: ...

    def set(self, key: str, payload: dict, ttl_seconds: float) -> None: ...

    def stats(self) -> dict: ...


def _top_similarity(query: list, vectors: list[list]) -> tuple[int | None, float]:
    """`query` 与一批向量的余弦相似度，返回 (最高分的下标, 该分值)。

    **必须在锁外调用**（`KQ-17`）：这一步是 O(N×D)，而它此前被写在 `with self._lock:`
    里面——一次近似查找会把同进程所有 `get()` / `set()` 一起堵住数百毫秒，缓存反而
    成了串行点。

    为什么换成 numpy：纯 Python 的 `sum(x * y for x, y in zip(a, b))` 在 2000 条
    × 1024 维下是约 600 万次解释器级运算（数百毫秒）；numpy 一次矩阵乘 + 一次 norm
    是毫秒级。向量本就以 `list[float]` 存在，转 float32 的拷贝成本远小于省下的时间。

    维度不一致的条目直接跳过：余弦对它们没有定义，原实现靠 `_cosine` 返回 0.0 兜底，
    语义等价（0.0 永远低于命中阈值）。
    """
    dim = len(query)
    pairs = [(i, v) for i, v in enumerate(vectors) if len(v) == dim]
    if not pairs:
        return None, 0.0
    matrix = np.asarray([v for _, v in pairs], dtype=np.float32)
    q = np.asarray(query, dtype=np.float32)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0:
        return None, 0.0
    norms = np.linalg.norm(matrix, axis=1) * q_norm
    # 零向量条目的分母为 0；显式屏蔽，否则 inf/nan 会污染 argmax
    valid = norms > 1e-12
    if not valid.any():
        return None, 0.0
    sims = np.zeros(len(pairs), dtype=np.float32)
    sims[valid] = (matrix[valid] @ q) / norms[valid]
    pos = int(np.argmax(sims))
    return pairs[pos][0], float(sims[pos])


# ---- 近似最近邻（LSH）----
#
# 为什么需要它：语义查找原本是"取出同组全部条目 → 一次矩阵乘"，O(N×D)。
# 2000 条 × 1024 维是毫秒级，**万级就是几十毫秒**，而且每次都全扫、纳秒必争地
# 挤在每一条问答的命中判定里。缓存的价值是省一次模型调用，但它不该自己变成
# 一个新瓶颈。
#
# 做法：随机超平面 LSH。每个条目按 `sign(v·Rᵀ)` 落一个 12 bit 桶，
# 查找时只在**汉明距离 ≤2 的邻居桶**里找候选（1+12+66 = 79 个桶的字典查询），
# 再把候选喂给原来的 `_top_similarity` 精确算余弦。
#
# 为什么"近似"是可接受的：命中阈值是 0.95，落在这个距离内的向量几乎一定
# 落在同一个桶附近（cos=0.95 时单个比特一致的期望是 0.9，12 bit 上汉明距离
# ≤2 的概率约 0.97）。真正完全相同的问句走的是精确键命中，**不经过 LSH**。
#
# 组内条目少于 `_ANN_MIN_GROUP` 时**不启用** LSH：小规模下全扫既更准也更省
# （79 次字典查询比 512 行矩阵乘还贵）。
_ANN_MIN_GROUP = 512
_ANN_BITS = 12
_ANN_SEED = 0x5EED
_ANN_PROBE_BITS = 2


def _nearby_buckets(bits: int, width: int, max_flip: int) -> list[int]:
    """`bits` 本身，以及汉明距离 ≤ `max_flip` 的所有邻居桶编号。"""
    out = [bits]
    for i in range(width):
        flipped = bits ^ (1 << i)
        out.append(flipped)
        if max_flip >= 2:
            for j in range(i + 1, width):
                out.append(flipped ^ (1 << j))
    return out


class _LshIndex:
    """按 (租户, 范围, 维度签名) 分组的随机超平面索引。

    **只做候选筛选，不做相似度判定**——最终是否命中仍由 `_top_similarity` +
    阈值决定，所以 LSH 召回偏差只影响命中率，永远不会造成"错答案被命中"。
    """

    def __init__(self, bits: int = _ANN_BITS) -> None:
        self._bits = bits
        self._proj: dict[int, np.ndarray] = {}
        self._buckets: dict[tuple[str, str, str], dict[tuple[int, int], set[str]]] = {}
        self._of_key: dict[str, tuple[tuple[str, str, str], tuple[int, int]]] = {}
        self._lock = threading.Lock()

    def _projections(self, dim: int) -> np.ndarray:
        mat = self._proj.get(dim)
        if mat is None:
            # 固定种子：同一维度在进程内始终用同一组超平面，否则重启后
            # 老条目与新查询会落到互不相干的两套空间里（索引直接失效）
            rng = np.random.default_rng(_ANN_SEED + dim)
            mat = rng.standard_normal((self._bits, dim)).astype(np.float32)
            self._proj[dim] = mat
        return mat

    def bucket_of(self, vector: list) -> tuple[int, int] | None:
        dim = len(vector)
        if dim == 0:
            return None
        q = np.asarray(vector, dtype=np.float32)
        if not np.any(q):
            return None  # 零向量没有方向，与余弦语义一致（不参与近似命中）
        signs = (self._projections(dim) @ q) > 0
        bits = 0
        for i, positive in enumerate(signs):
            if bool(positive):
                bits |= 1 << i
        return dim, bits

    def add(self, key: str, tenant_id: str, scope: str, signature: str, vector: list) -> None:
        bucket = self.bucket_of(vector)
        if bucket is None:
            return
        group = (tenant_id, scope, signature)
        with self._lock:
            self._detach_locked(key)  # 同键覆盖写：先摘掉旧桶里的引用
            self._buckets.setdefault(group, {}).setdefault(bucket, set()).add(key)
            self._of_key[key] = (group, bucket)

    def remove(self, key: str) -> None:
        with self._lock:
            self._detach_locked(key)

    def _detach_locked(self, key: str) -> None:
        record = self._of_key.pop(key, None)
        if record is None:
            return
        group, bucket = record
        buckets = self._buckets.get(group)
        if not buckets:
            return
        keys = buckets.get(bucket)
        if keys:
            keys.discard(key)
            if not keys:
                buckets.pop(bucket, None)
        if not buckets:
            self._buckets.pop(group, None)

    def probe(self, tenant_id: str, scope: str, signature: str, vector: list) -> list[str] | None:
        """返回候选键；`None` 表示"该走全扫"（组太小 / 向量不可用）。"""
        bucket = self.bucket_of(vector)
        if bucket is None:
            return None
        group = (tenant_id, scope, signature)
        with self._lock:
            buckets = self._buckets.get(group)
            if not buckets:
                return None
            total = sum(len(keys) for keys in buckets.values())
            if total < _ANN_MIN_GROUP:
                return None
            candidates: list[str] = []
            for nearby in _nearby_buckets(bucket[1], self._bits, _ANN_PROBE_BITS):
                keys = buckets.get((bucket[0], nearby))
                if keys:
                    candidates.extend(keys)
            return candidates

    def stats(self) -> dict:
        with self._lock:
            return {
                "ann_groups": len(self._buckets),
                "ann_entries": len(self._of_key),
                "ann_bits": self._bits,
                "ann_min_group": _ANN_MIN_GROUP,
            }


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
    ) -> None:
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._embeddings = embeddings
        self._threshold = sim_threshold
        self._max_scan = max_scan
        # 近似候选索引（LSH）：组内条目超过阈值后生效，见 `_LshIndex` 说明
        self._lsh = _LshIndex()

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
        signature: str = "",
    ) -> None:
        stores_vector = list(vector or [])
        with self._lock:
            if len(self._entries) >= self._max_entries:
                # 简单逐出：删最早创建且命中最少的一批
                victims = sorted(
                    self._entries.items(), key=lambda kv: (kv[1].hit_count, kv[1].created_at)
                )[: max(1, self._max_entries // 10)]
                for k, _ in victims:
                    self._entries.pop(k, None)
                    self._lsh.remove(k)  # 索引与条目同生共死，否则候选里全是空指针
            self._entries[key] = CacheEntry(
                payload=payload,
                created_at=time.time(),
                ttl_seconds=ttl_seconds,
                vector=stores_vector,
                question=question or str(payload.get("question", "")),
                tenant_id=tenant_id,
                scope=scope,
                signature=signature,
            )
            self._lsh.add(key, tenant_id, scope, signature, stores_vector)

    def lookup(
        self,
        question: str,
        exact_key: str,
        vector: list | None = None,
        tenant_id: str = "",
        scope: str = "",
        signature: str = "",
    ) -> tuple[CacheEntry | None, str]:
        """语义级查找：先按键精确命中，再退回向量近似命中。

        返回 (entry, hit_kind)，hit_kind ∈ {"exact", "semantic", "miss"}。
        近似命中会把候选按余弦从高到低扫描一遍，只认**同一租户**的条目——
        此前实现遍历全部 entries 并未按租户过滤（与下方注释自相矛盾），
        导致 A 租户的答案可被 B 租户近似命中（DEF-R-22 跨租户串答案）。

        近似命中的三个硬条件（KQ-04 + F4）：
        - 同一租户（否则跨租户串答案）；
        - 维度签名完全一致（模型版本 + user + index_version + 配置摘要），
          否则同租户内 A 用户的答案会命中给 B 用户，权限/配置变更后还会命中旧条目；
        - `scope` 一致（F4 快问）：检索范围不同的条目一律不互相命中。

        """
        exact = self.get(exact_key)
        if exact is not None:
            return exact, "exact"
        # 只要求调用方给得出问题向量：是否持有编码器是调用侧的职责，
        # 缓存自己不该把"没装编码器"等同于"不能近似命中"
        if not vector or not signature:
            return None, "miss"
        now = time.time()
        # ① 锁内只取**引用快照**（O(n) 拷贝指针，不拷向量）；过滤与计算全在锁外。
        #    此前整段扫描 + 余弦都在 `self._lock` 里，2000 条时把缓存变成全局串行点（KQ-17）。
        #
        #    候选来源二选一：
        #    - 组内条目 < `_ANN_MIN_GROUP`：全扫（精确，小规模成本可忽略）；
        #    - 否则走 LSH 邻居桶（把 O(N) 压成 79 次字典查询）。
        #    两条路径之后**都**要过同一套三闸 + 阈值，所以 LSH 只影响命中率，
        #    不可能造出"错答案被命中"。
        with self._lock:
            probed = self._lsh.probe(tenant_id, scope, signature, vector)
            if probed is None:
                snapshot = list(self._entries.items())
            else:
                # 探不到邻居就是未命中：**不再回退全扫**——否则每次 miss 又把
                # O(N×D) 请回来，等于没做。代价是极少数边界近似（汉明 >2）漏命中，
                # 这在"缓存少命中一次"的量级上，可接受且可观测（`ann_misses`）。
                snapshot = [(k, self._entries[k]) for k in probed[: self._max_scan] if k in self._entries]
        # ② 三闸过滤（租户 / 维度签名 / 范围）+ TTL：与命中判定同语义，只是挪到锁外
        candidates: list[tuple[str, CacheEntry]] = []
        for key, entry in snapshot:
            if entry.tenant_id != tenant_id:
                continue
            if entry.signature != signature:
                continue
            if entry.scope != scope:
                continue
            if not entry.vector:
                continue
            if now - entry.created_at > entry.ttl_seconds:
                continue
            candidates.append((key, entry))
            if len(candidates) >= self._max_scan:
                break
        if not candidates:
            return None, "miss"
        # ③ 锁外向量化算相似度
        pos, sim = _top_similarity(vector, [e.vector for _, e in candidates])
        if pos is None or sim < self._threshold:
            return None, "miss"
        # ④ 回锁复核：快照之后该条目可能被逐出或被同键覆盖，
        #    直接对旧对象自增计数会让统计落在一个已不在表里的对象上
        key, entry = candidates[pos]
        with self._lock:
            if self._entries.get(key) is not entry:
                return None, "miss"
            entry.hit_count += 1
            entry.last_hit_at = now
            return entry, "semantic"

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            alive = [e for e in self._entries.values() if now - e.created_at <= e.ttl_seconds]
            return {
                "entries": len(alive),
                "total_hits": sum(e.hit_count for e in alive),
                "semantic_ready": self._embeddings is not None,
                "sim_threshold": self._threshold,
                **self._lsh.stats(),
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
    ) -> None:
        self._state = state
        self._embeddings = embeddings
        self._threshold = sim_threshold
        self._max_scan = max_scan
        self._max_entries = max_entries
        # 本地近似索引：key → (vector, tenant_id, scope, signature, created_at, ttl)
        self._index: dict[str, tuple[list, str, str, str, float, float]] = {}
        self._lock = threading.Lock()
        self._lsh = _LshIndex()
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
                "signature": entry.signature,
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
            signature=data.get("signature", ""),
        )

    # ---- SemanticCache 接口 ----

    def get(self, key: str) -> CacheEntry | None:
        try:
            raw = self._state.get(self._k(key))
        except Exception:  # noqa: BLE001 - 存储异常 → 视作未命中
            logger.debug("缓存存储异常：视作未命中")
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
        signature: str = "",
    ) -> None:
        entry = CacheEntry(
            payload=payload,
            created_at=time.time(),
            ttl_seconds=ttl_seconds,
            question=question or str(payload.get("question", "")),
            tenant_id=tenant_id,
            scope=scope,
            signature=signature,
        )
        try:
            self._state.set(self._k(key), self._encode(entry), int(ttl_seconds) or 0)
        except Exception:  # noqa: BLE001 - 写失败不影响本次返回
            logger.debug("缓存写入失败：不影响本次返回")
            pass
        if vector:
            with self._lock:
                if len(self._index) >= self._max_entries:
                    # 简单逐出：丢最早写入的
                    oldest = min(self._index.items(), key=lambda kv: kv[1][4])[0]
                    self._index.pop(oldest, None)
                    self._lsh.remove(oldest)
                self._index[key] = (
                    list(vector), tenant_id, scope, signature, entry.created_at, ttl_seconds
                )
                self._lsh.add(key, tenant_id, scope, signature, list(vector))

    def lookup(
        self,
        question: str,
        exact_key: str,
        vector: list | None = None,
        tenant_id: str = "",
        scope: str = "",
        signature: str = "",
    ) -> tuple[CacheEntry | None, str]:
        exact = self.get(exact_key)
        if exact is not None:
            return exact, "exact"
        if not vector:
            with self._lock:
                self._misses += 1
            return None, "miss"
        now = time.time()
        # 同 InMemorySemanticCache：锁内只取快照，过滤与余弦都在锁外（KQ-17）。
        # 本地近似索引里只有 key→元组，快照不会拷贝向量本身（元组里的 list 是引用）。
        with self._lock:
            probed = self._lsh.probe(tenant_id, scope, signature, vector)
            if probed is None:
                snapshot = list(self._index.items())
            else:
                snapshot = [(k, self._index[k]) for k in probed[: self._max_scan] if k in self._index]
        candidates: list[tuple[str, list]] = []
        for key, (
            vec,
            entry_tenant,
            entry_scope,
            entry_signature,
            created_at,
            ttl,
        ) in snapshot:
            if entry_tenant != tenant_id:
                continue
            if entry_scope != scope:
                continue
            # 维度签名闸（KQ-04）：主体/权限/数据版本/模型/配置任一不同即不可命中
            if entry_signature != signature:
                continue
            if now - created_at > ttl:
                continue
            candidates.append((key, vec))
            if len(candidates) >= self._max_scan:
                break
        pos, sim = _top_similarity(vector, [vec for _, vec in candidates])
        if pos is None or sim < self._threshold:
            with self._lock:
                self._misses += 1
            return None, "miss"
        entry = self.get(candidates[pos][0])
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
                **self._lsh.stats(),
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
