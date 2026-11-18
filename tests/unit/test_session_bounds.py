"""会话存档的两处无界增长（`KQ-23` 残余）。

## 为什么值得单独钉

① `turns` 此前是**无上限 append**，而每轮都带完整问答原文（长答案 2~8KB）。
   100 轮的会话快照能到数百 KB，且 `save_turn` 每次全量重写这个 JSON。
   裁剪必须**记数**（`trimmed_turns`）——否则"只剩 40 轮"会被误判成丢数据。

② `_locks` 只在 `delete()` 时回收。会话 30 天才过期、过期也可能没人删，
   于是锁字典随会话数无界增长（每把锁一个 threading.Lock，值不大但永不释放）。
   回收有两个不能违反的前提：**无人持有**、**确实空闲**——刚取出的锁被删掉会
   让同一会话出现两把锁，串行化直接失效（比不回收更糟）。
"""
import time

from app.services.agent.sessions import (
    LOCK_IDLE_S,
    LOCK_PRUNE_EVERY,
    MAX_TURNS_PER_SESSION,
    SessionStore,
)


def _save(store: SessionStore, session_id: str, n: int) -> None:
    for i in range(n):
        store.save_turn(
            session_id, "t1", "u1", f"问题{i}", f"答案{i}", "answered", {},
        )


def test_turns_are_trimmed_to_limit_and_counted(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create("t1", "u1")["session_id"]
    overflow = 5
    _save(store, sid, MAX_TURNS_PER_SESSION + overflow)

    session = store.get(sid, "t1", "u1")
    turns = session["turns"]
    assert len(turns) == MAX_TURNS_PER_SESSION
    # 保留的是最近 N 轮（旧轮次先丢），且丢弃条数可查
    assert turns[-1]["question"] == f"问题{MAX_TURNS_PER_SESSION + overflow - 1}"
    assert turns[0]["question"] == f"问题{overflow}"
    assert session["trimmed_turns"] == overflow


def test_trimmed_turns_accumulates_across_writes(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create("t1", "u1")["session_id"]
    _save(store, sid, MAX_TURNS_PER_SESSION + 3)
    _save(store, sid, 2)
    session = store.get(sid, "t1", "u1")
    # 3 + 2 = 5：累计值必须跨两次写操作继续增长
    assert session["trimmed_turns"] == 5
    assert len(session["turns"]) == MAX_TURNS_PER_SESSION


def test_idle_locks_are_reclaimed(tmp_path):
    store = SessionStore(tmp_path)
    store._lock_for("t1:u1:old-a")
    store._lock_for("t1:u1:old-b")
    for sid in list(store._locks_used_at):
        store._locks_used_at[sid] = time.time() - LOCK_IDLE_S - 10

    # 差一次到清理周期，再取一个新锁触发
    store._lock_calls = LOCK_PRUNE_EVERY - 1
    store._lock_for("t1:u1:fresh")

    assert "t1:u1:old-a" not in store._locks
    assert "t1:u1:old-b" not in store._locks
    assert "t1:u1:fresh" in store._locks
    # 使用时刻表必须同步清理，否则下次判断会读到孤儿时间戳
    assert "t1:u1:old-a" not in store._locks_used_at


def test_held_lock_is_never_reclaimed(tmp_path):
    store = SessionStore(tmp_path)
    held = store._lock_for("t1:u1:held")
    store._locks_used_at["t1:u1:held"] = time.time() - LOCK_IDLE_S - 10

    with held:  # 持有期间触发清理
        store._lock_calls = LOCK_PRUNE_EVERY - 1
        store._lock_for("t1:u1:other")
        assert "t1:u1:held" in store._locks

    # 释放后、且不再被取用时才会被下一轮清理回收
    assert "t1:u1:held" in store._locks
