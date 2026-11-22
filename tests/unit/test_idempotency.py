"""幂等（在被去重 + 结果重放）的单测。

对应改造：前端在 429/503 后会自动重试一次 → 同一问题被算两次钱，
且两次都"成功"，监控完全看不见这类损耗。
"""
from app.services.idempotency import DONE, INFLIGHT, NEW, IdempotencyStore, content_key


class TestContentKey:
    def test_stable(self):
        a = content_key("t1", "保修多久？", "s1")
        b = content_key("t1", "保修多久？", "s1")
        assert a == b

    def test_differs_by_tenant_and_question(self):
        assert content_key("t1", "q") != content_key("t2", "q")
        assert content_key("t1", "q1") != content_key("t1", "q2")

    def test_question_not_in_key(self):
        """键里不能出现用户原话（会进日志与键空间）。"""
        key = content_key("t1", "机密问题内容", "s1")
        assert "机密" not in key
        assert len(key) == 32


class TestInflightDedup:
    def test_second_acquire_is_rejected(self):
        store = IdempotencyStore()
        assert store.acquire("k") is True
        assert store.acquire("k") is False

    def test_release_allows_next(self):
        store = IdempotencyStore()
        store.acquire("k")
        store.release("k")
        assert store.acquire("k") is True

    def test_release_is_idempotent(self):
        store = IdempotencyStore()
        store.release("k")  # 没占位就释放也不能炸
        store.release("k")


class TestIdempotencyKey:
    def test_first_is_new(self):
        store = IdempotencyStore()
        state, payload = store.begin("k")
        assert state == NEW
        assert payload is None

    def test_completed_result_is_replayed(self):
        store = IdempotencyStore()
        store.begin("k")
        store.complete("k", {"status": "ok", "data": {"answer": "12 个月"}})
        state, payload = store.begin("k")
        assert state == DONE
        assert payload["data"]["answer"] == "12 个月"

    def test_failure_leaves_no_replayable_result(self):
        """失败不该留下可回放结果——否则客户端拿到的是错误快照。"""
        store = IdempotencyStore()
        store.begin("k")
        store.fail("k")
        state, payload = store.begin("k")
        assert state == NEW
        assert payload is None

    def test_concurrent_same_key_is_inflight(self):
        store = IdempotencyStore()
        store.begin("k")
        state, _ = store.begin("k")
        assert state == INFLIGHT


