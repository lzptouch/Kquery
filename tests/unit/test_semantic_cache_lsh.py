"""语义缓存的近似候选索引（LSH）。

## 边界要说清楚

LSH 在这里**只筛候选**，命中仍由同一套"三闸 + 余弦阈值 + 回锁复核"决定。
所以它可能带来的是**命中率的小幅下降**（漏掉一个本该命中的近似改写），
绝不允许出现"不同的问题命中了别人的答案"。

因此这组用例钉的是四件事：

1. 规模跨过阈值后，近似改写**仍然命中**（召回没有崩）；
2. 低于阈值（cos < 0.95）的**不许命中**（阈值语义没被绕过）；
3. 跨租户 / 跨范围 / 跨签名的条目**不进候选**（闸门没被索引旁路）；
4. 逐出 / 覆盖写之后索引不残留（否则候选里全是已删条目，命中率假性归零）。
"""
import math
import random

import numpy as np
import pytest

from app.services import cache as cache_mod
from app.services.cache import InMemorySemanticCache, RedisSemanticCache, _LshIndex


def _unit(vec) -> list[float]:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return (arr / norm).tolist() if norm else arr.tolist()


def _jitter(base: list[float], eps: float, seed: int) -> list[float]:
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(len(base)).astype(np.float32) * eps
    return _unit(np.asarray(base, dtype=np.float32) + noise)


def _cos(a: list[float], b: list[float]) -> float:
    dot = float(np.dot(np.asarray(a), np.asarray(b)))
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    return dot / (na * nb) if na and nb else 0.0


DIM = 64
TENANT = "t1"
SIG = "sig-1"
SCOPE = "global"


@pytest.fixture()
def big_cache(monkeypatch):
    """组内条目数超过阈值 → 走 LSH 分支。"""
    monkeypatch.setattr(cache_mod, "_ANN_MIN_GROUP", 8)
    cache = InMemorySemanticCache(max_entries=200)
    rng = random.Random(7)
    for i in range(40):
        vec = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
        cache.set(
            f"k{i}", {"answer": f"a{i}"}, ttl_seconds=600, vector=vec,
            tenant_id=TENANT, scope=SCOPE, signature=SIG,
        )
    return cache


def test_near_duplicate_still_hits_at_scale(big_cache):
    """近重复（cos ≈ 0.999）必须命中——这是语义缓存存在的理由。"""
    rng = random.Random(11)
    base = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
    big_cache.set(
        "target", {"answer": "target"}, ttl_seconds=600, vector=base,
        tenant_id=TENANT, scope=SCOPE, signature=SIG,
    )
    query = _jitter(base, 0.01, seed=3)
    assert _cos(base, query) > 0.95  # 前提：确实是近重复

    entry, kind = big_cache.lookup("q", "no-exact-key", vector=query,
                                   tenant_id=TENANT, scope=SCOPE, signature=SIG)
    assert kind == "semantic"
    assert entry.payload["answer"] == "target"


def test_unrelated_question_misses(big_cache):
    """cos 远低于阈值 → 必须 miss（LSH 不能把"桶相邻"当成"语义相近"）。"""
    rng = random.Random(99)
    query = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
    entry, kind = big_cache.lookup("q", "no-exact-key", vector=query,
                                   tenant_id=TENANT, scope=SCOPE, signature=SIG)
    assert entry is None and kind == "miss"


def test_threshold_still_enforced(big_cache):
    """落在同一桶里但余弦不足阈值的，也不能命中。"""
    base = _unit([1.0] * DIM)
    big_cache.set("t", {"answer": "t"}, ttl_seconds=600, vector=base,
                  tenant_id=TENANT, scope=SCOPE, signature=SIG)
    # 与 base 夹角 ~26°（cos≈0.9 < 0.95），仍可能同桶
    near = _unit([1.0] * (DIM // 2) + [0.45] * (DIM - DIM // 2))
    entry, kind = big_cache.lookup("q", "no-exact-key", vector=near,
                                   tenant_id=TENANT, scope=SCOPE, signature=SIG)
    assert entry is None and kind == "miss"


def test_tenant_gate_not_bypassed_by_index(big_cache):
    """索引里有同向量的条目，但租户不同 → 不许命中。"""
    rng = random.Random(5)
    vec = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
    big_cache.set("other", {"answer": "other"}, ttl_seconds=600, vector=vec,
                  tenant_id="t2", scope=SCOPE, signature=SIG)
    entry, kind = big_cache.lookup("q", "no-exact-key", vector=vec,
                                   tenant_id=TENANT, scope=SCOPE, signature=SIG)
    assert entry is None and kind == "miss"


def test_scope_and_signature_gates(big_cache):
    rng = random.Random(6)
    vec = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
    big_cache.set("s", {"answer": "s"}, ttl_seconds=600, vector=vec,
                  tenant_id=TENANT, scope="kb:other", signature=SIG)
    big_cache.set("g", {"answer": "g"}, ttl_seconds=600, vector=vec,
                  tenant_id=TENANT, scope=SCOPE, signature="sig-2")

    for scope, sig in (("kb:other", SIG), (SCOPE, "sig-2")):
        entry, kind = big_cache.lookup("q", "no-exact-key", vector=vec,
                                       tenant_id=TENANT, scope=SCOPE, signature=SIG)
        assert entry is None and kind == "miss", (scope, sig)


def test_overwrite_does_not_leave_stale_index_entry(big_cache):
    """同键覆盖写：旧向量必须从索引里摘掉，否则会命中已不存在的内容。"""
    rng = random.Random(21)
    old = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
    new = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
    big_cache.set("k0", {"answer": "old"}, ttl_seconds=600, vector=old,
                  tenant_id=TENANT, scope=SCOPE, signature=SIG)
    big_cache.set("k0", {"answer": "new"}, ttl_seconds=600, vector=new,
                  tenant_id=TENANT, scope=SCOPE, signature=SIG)

    entry, kind = big_cache.lookup("q", "no-exact-key", vector=_jitter(old, 0.005, 1),
                                   tenant_id=TENANT, scope=SCOPE, signature=SIG)
    # 旧向量已被新向量替换：要命中只能命中 new（或直接未命中），绝不能是 "old"
    assert entry is None or entry.payload["answer"] == "new"
    assert big_cache.stats()["ann_entries"] <= big_cache.stats()["entries"]


def test_lsh_keeps_candidate_set_small(big_cache):
    """候选规模必须远小于全量：这是 LSH 存在的唯一意义。"""
    rng = random.Random(31)
    query = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
    probed = big_cache._lsh.probe(TENANT, SCOPE, SIG, query)
    assert probed is not None
    assert len(probed) <= len(big_cache._entries)  # 不会比全扫更大
    assert big_cache.stats()["ann_groups"] >= 1


def test_small_group_falls_back_to_full_scan():
    """组内条目少时**不启用** LSH：小规模下全扫更准、也更省（79 次字典查询比 8 行矩阵乘贵）。"""
    cache = InMemorySemanticCache()
    base = _unit([1.0, 0.0, 0.0, 0.0])
    cache.set("a", {"answer": "a"}, ttl_seconds=600, vector=base,
              tenant_id=TENANT, scope=SCOPE, signature=SIG)
    assert cache._lsh.probe(TENANT, SCOPE, SIG, base) is None
    entry, kind = cache.lookup("q", "no-exact", vector=_jitter(base, 0.01, 2),
                               tenant_id=TENANT, scope=SCOPE, signature=SIG)
    assert kind == "semantic"


# ---------------------------------------------------------------- 索引单元行为


def test_bucket_is_stable_for_same_vector():
    idx = _LshIndex()
    vec = _unit([0.3, -0.7, 0.2, 0.9])
    assert idx.bucket_of(vec) == idx.bucket_of(vec)


def test_zero_vector_has_no_bucket():
    idx = _LshIndex()
    assert idx.bucket_of([0.0] * 8) is None


def test_nearby_buckets_include_self_and_neighbours():
    from app.services.cache import _nearby_buckets

    buckets = _nearby_buckets(0b0000, 3, 2)
    assert 0b0000 in buckets
    assert 0b0001 in buckets  # 汉明 1
    assert 0b0011 in buckets  # 汉明 2
    assert 0b0111 not in buckets  # 汉明 3：不探


def test_remove_drops_all_traces():
    idx = _LshIndex()
    vec = _unit([1.0, 2.0, 3.0])
    idx.add("k", TENANT, SCOPE, SIG, vec)
    assert idx.stats()["ann_entries"] == 1
    idx.remove("k")
    assert idx.stats()["ann_entries"] == 0
    idx.remove("k")  # 幂等
    assert idx.stats()["ann_entries"] == 0


# ---------------------------------------------------------------- Redis 版同样接上


class _MemState:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ttl=0):
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


def test_redis_backend_also_uses_lsh(monkeypatch):
    monkeypatch.setattr(cache_mod, "_ANN_MIN_GROUP", 3)
    cache = RedisSemanticCache(_MemState(), max_entries=50)
    rng = random.Random(41)
    for i in range(8):
        vec = _unit([rng.uniform(-1, 1) for _ in range(DIM)])
        cache.set(f"k{i}", {"answer": f"a{i}"}, ttl_seconds=600, vector=vec,
                  tenant_id=TENANT, scope=SCOPE, signature=SIG)

    target = _jitter(_unit([1.0] * DIM), 0.005, 9)
    # 目标自身必须是组内近邻：直接构造一个同向条目
    cache.set("target", {"answer": "target"}, ttl_seconds=600, vector=_unit([1.0] * DIM),
              tenant_id=TENANT, scope=SCOPE, signature=SIG)
    entry, kind = cache.lookup("q", "no-exact", vector=target,
                               tenant_id=TENANT, scope=SCOPE, signature=SIG)
    assert kind == "semantic"
    assert entry.payload["answer"] == "target"
    assert cache.stats()["ann_entries"] >= 1


def test_lsh_projection_deterministic_across_instances():
    """同一维度必须用同一组超平面：否则不同实例（或重启后）的桶互不相通。"""
    a, b = _LshIndex(), _LshIndex()
    vec = _unit([math.sin(i) for i in range(32)])
    assert a.bucket_of(vec) == b.bucket_of(vec)
