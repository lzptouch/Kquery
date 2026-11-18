"""状态外置（批次 4）的回归测试。

**为什么这些断言重要**：多副本语义此前是"看起来能用、实际会裂"——
会话在副本 A 建了、切到副本 B 就"不存在"；灰度配置写在 A 的磁盘上、B 看不见
（→ 灰度"时灵时不灵"，实验数据无法归因）。
所以测试必须用**两个 store 实例共享一份共享状态**来模拟两个副本。
"""
import time

from app.core.shared_state import SharedState
from app.services.external_state import ExternalDocStore


def _state() -> SharedState:
    """共享状态（进程内后端即可——Redis 不可用时的降级路径也要正确）。"""
    return SharedState()


class TestExternalDocStore:
    def test_put_and_get(self):
        docs = ExternalDocStore(_state(), "unit")
        docs.put("a", {"title": "T"}, index="idx", score=1.0)
        assert docs.get("a")["title"] == "T"

    def test_missing_returns_none(self):
        docs = ExternalDocStore(_state(), "unit")
        assert docs.get("nope") is None

    def test_index_listing_is_newest_first(self):
        docs = ExternalDocStore(_state(), "unit")
        docs.put("a", {"v": 1}, index="idx", score=1.0)
        docs.put("b", {"v": 2}, index="idx", score=2.0)
        assert docs.list_ids("idx")[0] == "b"

    def test_delete_removes_doc_and_index(self):
        docs = ExternalDocStore(_state(), "unit")
        docs.put("a", {"v": 1}, index="idx", score=1.0)
        docs.delete("a", index="idx")
        assert docs.get("a") is None
        assert "a" not in docs.list_ids("idx")

    def test_version_increments_and_detects_conflict(self):
        """乐观并发：别人写过的版本必须让本次写入被拒（否则丢更新）。"""
        docs = ExternalDocStore(_state(), "unit")
        docs.put("a", {"v": 1})
        v = docs.version_of("a")
        docs.put("a", {"v": 2})
        assert docs.version_of("a") > v
        # 用**旧版本**写入 → 冲突 → 拒绝
        assert docs.put("a", {"v": 3}, expect_version=v) is False
        assert docs.get("a")["v"] == 2
        # 用当前版本写入 → 成功
        assert docs.put("a", {"v": 4}, expect_version=docs.version_of("a")) is True

    def test_degraded_flag(self):
        """未配 Redis → 降级到进程内，degraded 必须为真（多副本语义已失效，要可观测）。"""
        docs = ExternalDocStore(_state(), "unit")
        assert docs.degraded() is True
        assert docs.backend() == "local"


class TestSessionExternalization:
    def _store(self, state):
        import tempfile
        from pathlib import Path

        from app.services.agent.sessions import SessionStore

        return SessionStore(
            Path(tempfile.mkdtemp()), docs=ExternalDocStore(state, "session")
        )

    def test_second_replica_sees_the_session(self):
        state = _state()
        a = self._store(state)
        b = self._store(state)          # 模拟另一个副本
        session = a.create("t1", "u1", "标题")
        assert len(b.list_sessions("t1", "u1")) == 1
        assert b.get(session["session_id"], "t1", "u1")["title"] == "标题"

    def test_turn_is_shared(self):
        state = _state()
        a = self._store(state)
        b = self._store(state)
        session = a.create("t1", "u1")
        a.save_turn(session["session_id"], "t1", "u1", "问题", "答案", "ok", {"evidence": []})
        assert b.get(session["session_id"], "t1", "u1")["turn"] == 1

    def test_delete_propagates(self):
        state = _state()
        a = self._store(state)
        b = self._store(state)
        session = a.create("t1", "u1")
        a.delete(session["session_id"], "t1", "u1")
        assert b.list_sessions("t1", "u1") == []

    def test_owner_isolation_still_enforced(self):
        """外置后**归属校验不能丢**（否则能读别人的会话）。"""
        state = _state()
        a = self._store(state)
        session = a.create("t1", "u1")
        try:
            a.get(session["session_id"], "t1", "other")
        except Exception as exc:  # noqa: BLE001
            assert "404" in str(exc) or "不属于" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("跨用户读取必须被拒")


class TestConfigExternalization:
    def test_scoped_overlay_visible_in_other_replica(self):
        """灰度覆盖层跨副本可见——否则灰度"时灵时不灵"，实验无法归因。"""
        import tempfile
        from pathlib import Path

        from app.domain.schemas import RetrievalConfig
        from app.services.config_store import RetrievalConfigStore

        state = _state()
        path = Path(tempfile.mkdtemp()) / "cfg.json"
        a = RetrievalConfigStore(path, docs=ExternalDocStore(state, "retrieval"))
        b = RetrievalConfigStore(path, docs=ExternalDocStore(state, "retrieval"))
        a.set_scoped("tenant:t1", RetrievalConfig(rrf_k=40), experiment_id="exp-1")
        config, label = b.resolve("t1", "u1")
        assert config.rrf_k == 40
        assert "exp-1" in label


class TestTaskExternalization:
    def test_task_visible_in_other_replica(self):

        from app.services.external_state import ExternalDocStore as Docs

        state = _state()
        # 只验证"文档层"能跨副本读到任务状态（TaskManager 依赖 knowledge，构造较重）
        docs = Docs(state, "ingest")
        docs.put("t1", {"task_id": "t1", "status": "running"}, index="tasks", score=1.0)
        other = Docs(state, "ingest")
        assert other.list_docs("tasks")[0]["status"] == "running"


class TestGraphTenantIsolation:
    def test_same_community_id_does_not_overwrite_across_tenants(self):
        """KQ-06：communities 主键此前只有 community_id → 跨租户互相覆盖。"""
        import tempfile
        from pathlib import Path

        from app.domain.schemas import Community
        from app.services.graph.sqlite_store import SqliteGraphStore

        db = Path(tempfile.mkdtemp()) / "g.db"
        store = SqliteGraphStore(db)
        store.set_community(
            Community(community_id="c1", tenant_id="t1", summary="A 租户")
        )
        store.set_community(
            Community(community_id="c1", tenant_id="t2", summary="B 租户")
        )
        t1 = [c.summary for c in store.communities("t1")]
        t2 = [c.summary for c in store.communities("t2")]
        assert "A 租户" in t1 and "B 租户" not in t1
        assert "B 租户" in t2 and "A 租户" not in t2


class TestCheckpointPrune:
    def test_prune_removes_old_rows(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from app.services.agent.checkpointer import prune_checkpoints

        db = Path(tempfile.mkdtemp()) / "cp.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE checkpoints (thread_id TEXT, created_at REAL, payload TEXT)"
        )
        conn.execute("INSERT INTO checkpoints VALUES ('a', 100.0, 'x')")
        conn.execute("INSERT INTO checkpoints VALUES ('b', ?, 'y')", (time.time(),))
        conn.commit()
        conn.close()

        deleted = prune_checkpoints(db, before_ts=time.time() - 60)
        assert deleted == 1
        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT thread_id FROM checkpoints").fetchall()
        conn.close()
        assert [r[0] for r in rows] == ["b"]

    def test_prune_missing_file_is_noop(self):
        import tempfile
        from pathlib import Path

        from app.services.agent.checkpointer import prune_checkpoints

        assert prune_checkpoints(Path(tempfile.mkdtemp()) / "none.db", 0) == 0
