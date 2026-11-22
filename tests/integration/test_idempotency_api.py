"""幂等在 API 层的验证（/ask 与 /ask/stream）。

对应改造：前端在 429/503 后会自动重试一次 → 同一问题被算两次钱。
"""
import threading
import time

from app.services.idempotency import content_key


class TestAskIdempotency:
    def test_duplicate_inflight_returns_409(self, client):
        """已有同问题在飞 → 立刻 409，不排队、不重复计费。"""
        idem = client.app.state.idempotency
        key = content_key("default", "保修多久？", "")
        assert idem.acquire(key) is True  # 模拟另一个请求正在处理
        try:
            resp = client.post("/ask", json={"question": "保修多久？"})
            assert resp.status_code == 409
            assert resp.json()["error"]["code"] == 2005
        finally:
            idem.release(key)

    def test_same_question_again_after_finish_is_allowed(self, client):
        """请求结束后必须释放占位：用户稍后再问同一个问题不能被自己的 409 挡住。"""
        first = client.post("/ask", json={"question": "保修多久？"})
        second = client.post("/ask", json={"question": "保修多久？"})
        assert first.status_code == 200
        assert second.status_code == 200

    def test_concurrent_duplicate_only_one_executes(self, client):
        """真实并发：两个相同问题同时进来，第二个必须被挡住（只算一次钱）。"""
        pipeline = client.app.state.pipeline
        original_generate = pipeline._llm.generate
        calls = {"n": 0}

        def slow_generate(prompt, *args, **kwargs):
            calls["n"] += 1
            time.sleep(0.4)
            return original_generate(prompt, *args, **kwargs)

        pipeline._llm.generate = slow_generate  # type: ignore[attr-defined]
        results: list = []

        def ask():
            try:
                results.append(
                    client.post(
                        "/ask", json={"question": "并发问题"}
                    ).status_code
                )
            except Exception:  # noqa: BLE001
                results.append(-1)

        threads = [threading.Thread(target=ask) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert 409 in results, f"并发重复请求应被挡住，实际状态码：{results}"
        assert calls["n"] <= 2, "被挡下的请求不得触发额外的模型调用"


class TestIdempotencyKeyReplay:
    def test_same_key_replays_previous_result(self, client):
        """同 key 的第二次请求直接回放（零调用），并带 X-Idempotent-Replay 标记。"""
        headers = {"Idempotency-Key": "req-001"}
        first = client.post("/ask", json={"question": "幂等问题"}, headers=headers)
        second = client.post("/ask", json={"question": "幂等问题"}, headers=headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.headers.get("X-Idempotent-Replay") == "true"
        assert second.json()["data"]["answer"] == first.json()["data"]["answer"]

    def test_different_keys_are_independent(self, client):
        h1 = {"Idempotency-Key": "req-a"}
        h2 = {"Idempotency-Key": "req-b"}
        r1 = client.post("/ask", json={"question": "问题甲"}, headers=h1)
        r2 = client.post("/ask", json={"question": "问题甲"}, headers=h2)
        assert r1.status_code == 200 and r2.status_code == 200
        assert r2.headers.get("X-Idempotent-Replay") is None
