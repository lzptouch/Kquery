"""语义缓存的近似查找：正确性 + 不持全局锁（`KQ-17`）。

## 为什么单独立一个文件

近似查找是在**请求路径上**跑的：每次 `/ask` 未精确命中都要扫一遍向量索引。
旧实现把「扫描 2000 条 × 1024 维」整段写在 `with self._lock:` 里，后果有两层：

- 计算慢：纯 Python 的 `sum(x * y for ...)` 是数百万次解释器级运算（数百毫秒）；
- 更糟的是**持锁计算**——这段时间里同进程任何 `get()` / `set()` 全部排队，
  缓存从"加速器"变成"全局串行点"，而且症状是"偶发变慢"，很难归因到缓存。

所以除了算对，还要钉住：**计算期间其它线程必须能进缓存**。这条用monkeypatch
注入一个可控的慢函数 + 事件同步来测，不依赖真实耗时（否则就是 flaky 测试）。
"""
import threading
import time

from app.services.cache import InMemorySemanticCache


def _cache(threshold: float = 0.9) -> InMemorySemanticCache:
    return InMemorySemanticCache(sim_threshold=threshold)


def test_semantic_hit_updates_hit_count():
    cache = _cache()
    cache.set(
        "k1", {"id": "a"}, ttl_seconds=60, question="保修政策",
        vector=[1.0, 0.0], tenant_id="t1", signature="s1",
    )
    entry, kind = cache.lookup("保修条款", "no-such-key", vector=[1.0, 0.0], tenant_id="t1", signature="s1")
    assert kind == "semantic"
    assert entry is not None and entry.hit_count == 1
    # 计数落在表内对象上（回锁复核后再自增，不是对快照里的旧副本自增）
    assert cache._entries["k1"].hit_count == 1


def test_semantic_hit_respects_signature_and_scope():
    cache = _cache()
    cache.set("k1", {"id": "a"}, ttl_seconds=60, vector=[1.0, 0.0],
              tenant_id="t1", scope="doc-a", signature="s1")
    # 签名不同 → 不允许命中
    assert cache.lookup("q", "none", vector=[1.0, 0.0], tenant_id="t1",
                        scope="doc-a", signature="s2")[1] == "miss"
    # 范围不同 → 不允许命中
    assert cache.lookup("q", "none", vector=[1.0, 0.0], tenant_id="t1",
                        scope="doc-b", signature="s1")[1] == "miss"
    # 两者一致 → 命中
    assert cache.lookup("q", "none", vector=[1.0, 0.0], tenant_id="t1",
                        scope="doc-a", signature="s1")[1] == "semantic"


def test_mixed_vector_dimensions_are_skipped_not_fatal():
    """维度不一致的条目必须被跳过——旧实现靠 `_cosine` 返回 0.0 兜底，
    向量化后改为在构造矩阵前按维度过滤，语义必须等价。"""
    cache = _cache()
    cache.set("bad", {"id": "bad"}, ttl_seconds=60, vector=[1.0],
              tenant_id="t1", signature="s1")
    cache.set("good", {"id": "good"}, ttl_seconds=60, vector=[1.0, 0.0],
              tenant_id="t1", signature="s1")
    entry, kind = cache.lookup("q", "none", vector=[1.0, 0.0], tenant_id="t1", signature="s1")
    assert kind == "semantic"
    assert entry is not None and entry.payload["id"] == "good"


def test_zero_vector_entries_do_not_produce_nan():
    cache = _cache(threshold=0.5)
    cache.set("zero", {"id": "zero"}, ttl_seconds=60, vector=[0.0, 0.0],
              tenant_id="t1", signature="s1")
    cache.set("real", {"id": "real"}, ttl_seconds=60, vector=[1.0, 0.0],
              tenant_id="t1", signature="s1")
    entry, kind = cache.lookup("q", "none", vector=[1.0, 0.0], tenant_id="t1", signature="s1")
    # 零向量分母为 0，不能让 nan 抢走 argmax
    assert kind == "semantic"
    assert entry is not None and entry.payload["id"] == "real"


def test_scan_releases_lock_while_computing(monkeypatch):
    cache = _cache()
    cache.set("k1", {"id": "a"}, ttl_seconds=60, vector=[1.0, 0.0],
              tenant_id="t1", signature="s1")

    started = threading.Event()
    release = threading.Event()

    def slow_similarity(query, vectors):  # noqa: ARG001 - 签名要与被测函数一致
        started.set()
        release.wait(timeout=5)
        return 0, 1.0

    monkeypatch.setattr("app.services.cache._top_similarity", slow_similarity)

    outcome: dict = {}

    def run_lookup() -> None:
        outcome["result"] = cache.lookup(
            "q", "none", vector=[1.0, 0.0], tenant_id="t1", signature="s1"
        )

    worker = threading.Thread(target=run_lookup, daemon=True)
    worker.start()
    assert started.wait(timeout=2), "相似度计算没有开始"

    # 计算进行中：其它线程必须仍能读写缓存。若是旧实现（整段持锁），这里会阻塞到
    # release 之后，断言就会超时——这正是"缓存变串行点"的可观测形态。
    t0 = time.time()
    assert cache.get("k1") is not None
    cache.set("k2", {"id": "b"}, ttl_seconds=60, vector=[0.0, 1.0],
              tenant_id="t1", signature="s1")
    assert time.time() - t0 < 1.0

    release.set()
    worker.join(timeout=5)
    assert outcome["result"][1] == "semantic"


def test_entry_evicted_during_scan_is_reported_as_miss(monkeypatch):
    """快照 → 计算 → 回锁复核之间条目可能已被逐出。

    此时**不能再按快照里的旧对象返回**：那条数据已不在缓存里，返回它等于凭空
    复活一条已被淘汰的答案（且统计会落在孤儿对象上）。
    """
    cache = _cache(threshold=0.5)
    cache.set("k1", {"id": "a"}, ttl_seconds=60, vector=[1.0, 0.0],
              tenant_id="t1", signature="s1")

    def evicting_similarity(query, vectors):  # noqa: ARG001
        cache._entries.clear()
        return 0, 1.0

    monkeypatch.setattr("app.services.cache._top_similarity", evicting_similarity)
    entry, kind = cache.lookup("q", "none", vector=[1.0, 0.0], tenant_id="t1", signature="s1")
    assert entry is None
    assert kind == "miss"
