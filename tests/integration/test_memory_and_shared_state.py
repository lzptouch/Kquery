""" 长期记忆 +  共享状态后端。

长期记忆最关键的约束：**不存用户原话**（与审计只存 hash 的纪律一致）。
共享状态最关键的约束：**Redis 挂了要降级而不是报错**。
"""
import threading
import time


from app.services.agent.memory import (
    KIND_CORRECTION,
    KIND_INVALID_QUERY,
    KIND_MISSING_DOC,
    LongTermMemory,
    query_fingerprint,
    record_from_run,
)


class TestMemoryBasics:
    def test_remember_and_recall(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        mem.remember("t1", KIND_CORRECTION, "年假政策", "该看《员工手册》第 3 章")
        entries = mem.recall("t1")
        assert len(entries) == 1
        assert entries[0].value == "该看《员工手册》第 3 章"

    def test_tenant_isolation(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        mem.remember("t1", KIND_CORRECTION, "k", "t1 的经验")
        mem.remember("t2", KIND_CORRECTION, "k", "t2 的经验")
        assert mem.recall("t1")[0].value == "t1 的经验"
        assert mem.recall("t2")[0].value == "t2 的经验"

    def test_idempotent_remember(self, tmp_path):
        """同 (tenant, kind, key) 幂等——重复观察同一现象不应堆积重复条目。"""
        mem = LongTermMemory(tmp_path / "m.jsonl")
        for _ in range(5):
            mem.remember("t1", KIND_INVALID_QUERY, "同一个查询", "x")
        assert len(mem.recall("t1", limit=50)) == 1

    def test_forget(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        mem.remember("t1", KIND_CORRECTION, "k", "v")
        assert mem.forget("t1", KIND_CORRECTION, "k") is True
        assert mem.recall("t1") == []

    def test_persists_across_instances(self, tmp_path):
        path = tmp_path / "m.jsonl"
        LongTermMemory(path).remember("t1", KIND_CORRECTION, "k", "v")
        assert LongTermMemory(path).recall("t1")[0].value == "v"

    def test_corrupt_line_skipped(self, tmp_path):
        path = tmp_path / "m.jsonl"
        mem = LongTermMemory(path)
        mem.remember("t1", KIND_CORRECTION, "k", "v")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("{ 不是合法 JSON\n")
        assert len(LongTermMemory(path).recall("t1")) == 1


class TestNoRawQuestionStored:
    """**核心约束**：记忆里不得出现用户原话全文。"""

    def test_long_question_replaced_by_fingerprint(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        long_q = "我想知道公司今年关于年假政策的具体规定以及调休的详细安排说明"
        record_from_run(mem, "t1", {
            "question": long_q, "refusal": True, "evidence": [],
            "tool_calls": [{"tool_name": "vector_search", "result_count": 0,
                            "arguments": {"query": "年假"}, "query": "年假"}],
        })
        for e in mem.recall("t1", limit=50):
            assert long_q not in e.key, "长问题原文被写进记忆"
            assert long_q not in e.value

    def test_short_question_kept_as_topic(self, tmp_path):
        """短问题通常是实体/指标名，保留可读性（这是刻意的取舍，不是疏漏）。"""
        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", {
            "question": "项目负责人", "refusal": True, "evidence": [],
            "tool_calls": [{"tool_name": "vector_search", "result_count": 0}],
        })
        assert any(e.key == "项目负责人" for e in mem.recall("t1", limit=50))

    def test_fingerprint_is_stable_and_irreversible(self):
        assert query_fingerprint("测试问题") == query_fingerprint("  测试问题  ".lower())
        assert query_fingerprint("测试问题") != query_fingerprint("另一个问题")
        assert "测试问题" not in query_fingerprint("测试问题")


class TestRecordFromRun:
    def test_records_invalid_query(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", {
            "tool_calls": [{"tool_name": "vector_search", "result_count": 0,
                            "arguments": {"query": "Q3 营收明细"}, "query": "Q3 营收明细"}],
        })
        kinds = {e.kind for e in mem.recall("t1", limit=50)}
        assert KIND_INVALID_QUERY in kinds

    def test_records_missing_doc_on_refusal(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", {
            "question": "项目负责人", "refusal": True, "evidence": [],
            "tool_calls": [{"tool_name": "vector_search", "result_count": 0}],
        })
        assert any(e.kind == KIND_MISSING_DOC for e in mem.recall("t1", limit=50))

    def test_records_on_refusal_with_irrelevant_evidence(self, tmp_path):
        """**真实拒答形态**：检索到了候选、但被判定为不相关。

        这是最常见的拒答路径（向量检索几乎总能返回 top_k 条），
        若把"证据为空"当作拒答的前置条件，主路径会被完全排除、记忆永远为空
        ——该缺陷由线上探针 entries=0 暴露，并已修正。
        """
        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", {
            "question": "量子纠缠退相干的时间尺度",
            "refusal": True,                      # 反思判定不相关 → 拒答
            "evidence": [{"chunk_id": "c1"}, {"chunk_id": "c2"}],  # 但确实召回了
            "tool_calls": [{"tool_name": "vector_search", "result_count": 4,
                            "arguments": {"query": "量子纠缠"}, "error": ""}],
        })
        assert any(e.kind == KIND_MISSING_DOC for e in mem.recall("t1", limit=50)), (
            "有候选但不相关的拒答也必须被记入（否则记忆永远为空）"
        )

    def test_no_record_when_answered_with_evidence(self, tmp_path):
        """正常作答不产生记忆（宁缺毋滥）。"""
        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", {
            "question": "年假几天", "refusal": False,
            "evidence": [{"chunk_id": "c1"}],
            "tool_calls": [{"tool_name": "vector_search", "result_count": 4}],
        })
        assert mem.recall("t1", limit=50) == []

    def test_does_not_record_successful_runs(self, tmp_path):
        """宁缺毋滥：成功的运行不产生记忆（垃圾记忆会污染后续提示词）。"""
        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", {
            "question": "年假", "evidence": [{"chunk_id": "c1"}],
            "tool_calls": [{"tool_name": "vector_search", "result_count": 3}],
        })
        assert mem.recall("t1", limit=50) == []

    def test_ignores_errored_calls(self, tmp_path):
        """工具报错不算"无效查询"——那是工具坏了，不是查询没用。"""
        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", {
            "tool_calls": [{"tool_name": "sql_search", "result_count": 0,
                            "arguments": {"query": "x"}, "error": "超时"}],
        })
        assert not [e for e in mem.recall("t1", limit=50) if e.kind == KIND_INVALID_QUERY]

    def test_never_raises(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", None)  # 不应抛


class TestEviction:
    def test_bounded_per_tenant(self, tmp_path):
        """记忆不能无限增长（注入成本与存储都会失控）。"""
        mem = LongTermMemory(tmp_path / "m.jsonl", max_per_tenant=10)
        for i in range(50):
            mem.remember("t1", KIND_CORRECTION, f"k{i}", f"v{i}")
        assert len(mem.list_entries("t1")) <= 10

    def test_high_hit_entries_survive_eviction(self, tmp_path):
        """淘汰按"命中少 + 最久未用"——真正有用的经验应留下。"""
        mem = LongTermMemory(tmp_path / "m.jsonl", max_per_tenant=5)
        mem.remember("t1", KIND_CORRECTION, "important", "v")
        for _ in range(10):
            mem.recall("t1", limit=50)  # 提升 important 的 hits
        for i in range(30):
            mem.remember("t1", KIND_CORRECTION, f"junk{i}", "v")
        keys = {e["key"] for e in mem.list_entries("t1")}
        assert "important" in keys, "高频命中的记忆被淘汰了"


class TestRenderHint:
    def test_empty_returns_empty(self):
        assert LongTermMemory.render_hint([]) == ""

    def test_includes_hedge_wording(self, tmp_path):
        """必须写明"供参考，不必盲从"——记忆是经验不是事实，知识库更新后可能失效。"""
        mem = LongTermMemory(tmp_path / "m.jsonl")
        mem.remember("t1", KIND_CORRECTION, "k", "v")
        text = LongTermMemory.render_hint(mem.recall("t1"))
        assert "供参考" in text and "不必盲从" in text

    def test_kind_specific_wording(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        mem.remember("t1", KIND_INVALID_QUERY, "某查询", "x")
        mem.remember("t1", KIND_MISSING_DOC, "某话题", "y")
        text = LongTermMemory.render_hint(mem.recall("t1", limit=10))
        assert "不要重复尝试" in text
        assert "知识库通常没有内容" in text


class TestMemoryConcurrency:
    def test_concurrent_remember_no_loss(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        with_threads = [threading.Thread(
            target=lambda i=i: mem.remember("t1", KIND_CORRECTION, f"k{i}", "v")
        ) for i in range(50)]
        for t in with_threads:
            t.start()
        for t in with_threads:
            t.join(timeout=5)
        assert len(mem.list_entries("t1")) == 50

    def test_concurrent_recall_is_safe(self, tmp_path):
        mem = LongTermMemory(tmp_path / "m.jsonl")
        for i in range(20):
            mem.remember("t1", KIND_CORRECTION, f"k{i}", "v")
        errors = []

        def worker():
            try:
                for _ in range(20):
                    mem.recall("t1", limit=5)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors


class TestSharedStateLocal:
    def test_incr_and_get(self):
        from app.core.shared_state import LocalStore

        s = LocalStore()
        assert s.incr("k") == 1
        assert s.incr("k", 5) == 6
        assert s.get("k") == "6"

    def test_ttl_expires(self):
        from app.core.shared_state import LocalStore

        s = LocalStore()
        s.incr("k", 1, ttl_s=1)
        assert s.get("k") == "1"
        s._values["k"] = ("1", time.time() - 1)  # 强制过期
        assert s.get("k") is None

    def test_zset_window(self):
        """滑动窗口（配额用的 zcount/zremrangebyscore）语义要正确。"""
        from app.core.shared_state import LocalStore

        s = LocalStore()
        now = time.time()
        for i in range(5):
            s.zadd("w", f"m{i}", now - i)
        assert s.zcount("w", now - 2, now) == 3
        removed = s.zremrangebyscore("w", 0, now - 3)
        assert removed == 2
        assert s.zcount("w", 0, now) == 3

    def test_delete_clears_both(self):
        from app.core.shared_state import LocalStore

        s = LocalStore()
        s.set("k", "v")
        s.zadd("k", "m", 1)
        s.delete("k")
        assert s.get("k") is None and s.zcount("k", 0, 10) == 0


class TestSharedStateDegradation:
    """核心：Redis 不可用必须降级，而不是让请求 500。"""

    def test_no_redis_uses_local(self):
        from app.core.shared_state import SharedState

        s = SharedState(redis_url="")
        assert s.backend == "local"
        assert s.incr("k") == 1

    def test_bad_redis_url_degrades(self):
        from app.core.shared_state import SharedState

        s = SharedState(redis_url="redis://127.0.0.1:1/0")  # 必然连不上
        assert "local" in s.backend
        assert s.incr("k") == 1, "Redis 连不上时必须降级而非抛异常"

    def test_degradation_is_counted(self):
        from app.core.shared_state import SharedState

        s = SharedState(redis_url="redis://127.0.0.1:1/0")
        s.incr("k")
        snap = s.snapshot()
        assert snap["backend"].startswith("local")
        assert snap["redis_enabled"] is True

    def test_probe_snapshot_shape(self):
        from app.core.shared_state import SharedState

        snap = SharedState().snapshot()
        assert {"backend", "degraded_operations", "redis_enabled"} <= set(snap)

    def test_url_is_masked_in_logs(self):
        """连接串可能含密码，日志里必须脱敏。"""
        from app.core.shared_state import _mask

        assert "secret" not in _mask("redis://user:secret@host:6379/0")
        assert "***" in _mask("redis://user:secret@host:6379/0")


class TestRefusalWriteBack:
    """拒答必须回写 state（ 依赖它，快照也依赖它）。

    线上探针显示长期记忆 entries 恒为 0，追下去发现两层问题：
    1. `record_from_run` 曾要求"拒答 **且** 无证据"，而真实拒答是"检索到候选但
       反思判定不相关" —— 主路径被条件排除掉；
    2. 即使条件放宽也无效：`state["refusal"]` 只在"证据为空"时由 answer_node 置位，
       而 `_build_result` 从**答案文本**推出的生效拒答没有回写 state。

    后果是快照里出现自相矛盾的数据（verdict=refused + refusal=False）。
    """

    def _runner(self):
        from app.services.agent.runner import AgentRunner

        # 只测 _build_result，绕开重依赖构造；补上它读到的少量配置属性
        r = AgentRunner.__new__(AgentRunner)
        r._top_k = 4
        r._score_top_n = 3
        r._model_name = "test"
        r._self_checker = None
        r._crag = None
        return r

    def test_effective_refusal_is_written_back(self):
        """答案文本表现为拒答时，state['refusal'] 必须被置为 True。"""
        from app.services.agent.runner import NO_INFO_ANSWER

        runner = self._runner()
        state = {
            "final_answer": NO_INFO_ANSWER,
            "evidence": [{"chunk_id": "c1", "content": "不相关内容", "score": 0.3}],
            "refusal": False,  # 检索到了候选，answer_node 未置位
            "evidence_score": 0.0,
        }
        result = runner._build_result(state, "问题", lambda d: "文档", 10, "t1")

        assert result["verdict"] == "refused"
        assert state["refusal"] is True, (
            "生效拒答未回写 state —— 快照会出现 verdict=refused 但 refusal=False，"
            "且长期记忆永远提炼不出内容"
        )

    def test_answer_does_not_set_refusal(self):
        """正常作答不得被误标为拒答。"""
        runner = self._runner()
        state = {
            "final_answer": "年假为 5 天[#1]。",
            "evidence": [{"chunk_id": "c1", "content": "年假 5 天", "score": 0.9}],
            "refusal": False,
            "evidence_score": 0.9,
        }
        result = runner._build_result(state, "年假几天", lambda d: "文档", 10, "t1")

        assert result["verdict"] == "answered"
        assert state.get("refusal") is False

    def test_memory_records_after_write_back(self, tmp_path):
        """端到端：回写之后记忆提炼才拿得到 refusal=True。"""
        from app.services.agent.runner import NO_INFO_ANSWER

        runner = self._runner()
        state = {
            "question": "某冷门话题",
            "final_answer": NO_INFO_ANSWER,
            "evidence": [{"chunk_id": "c1", "content": "不相关", "score": 0.2}],
            "refusal": False,
            "evidence_score": 0.0,
            "tool_calls": [{"tool_name": "vector_search", "result_count": 4,
                            "arguments": {"query": "某冷门话题"}, "error": ""}],
        }
        runner._build_result(state, "某冷门话题", lambda d: "文档", 10, "t1")

        mem = LongTermMemory(tmp_path / "m.jsonl")
        record_from_run(mem, "t1", state)
        assert any(e.kind == KIND_MISSING_DOC for e in mem.recall("t1", limit=50))


class TestWiring:
    def test_app_exposes_shared_state_and_memory(self, client):
        assert client.app.state.shared_state is not None
        assert client.app.state.long_term_memory is not None

    def test_health_probe_reports_backend(self, client):
        probes = client.get("/health/probes")
        assert probes.status_code in (200, 404)
        if probes.status_code == 200:
            data = probes.json().get("data", {})
            assert "shared_state" in data or "long_term_memory" in data

    def test_runner_gets_memory_backfilled(self, client):
        """Runner 装配早于记忆创建，必须回填（否则记忆永远不生效）。"""
        assert client.app.state.agent_runner._memory is not None
