"""按会话删除 LangGraph 检查点（`KQ-23` 残余）。

## 为什么不能只删快照

`DELETE /sessions/{id}` 之前只删 JSON 快照，`agent_checkpoints.db` 里该会话每一轮
的完整 state（对话原文、证据、工具出入参）还在，等 7 天 TTL 才消失。对用户来说
"删了"和"没删"没有区别的地方，就是隐私与磁盘都还在泄漏。

## 两个容易写错的地方

1. **前缀必须转义**：thread_id 是 `{session_id}:{turn}`，如果直接
   `LIKE '{session_id}:%'`，`_` 与 `%` 会被当通配符——`s_1` 的删除会连带
   删掉 `sx1` 的会话。
2. **不是所有表都有 thread_id**：sqlite_master 里可能有别的表（例如 LangGraph
   版本升级留下的辅助表），按列名判断而不是硬编码表名，才能两边都不炸。
"""
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app.services.agent.checkpointer import (
    delete_session_threads,
    resolve_checkpoint_db_path,
    thread_id_for,
)


def _make_db(path: Path, thread_ids: list[str]) -> None:
    conn = sqlite3.connect(str(path))
    try:
        # 模拟 SqliteSaver 的两张主表 + 一张无关表
        conn.execute("CREATE TABLE checkpoints (thread_id TEXT, checkpoint_id TEXT)")
        conn.execute("CREATE TABLE writes (thread_id TEXT, idx INTEGER)")
        conn.execute("CREATE TABLE unrelated (name TEXT)")
        for i, tid in enumerate(thread_ids):
            conn.execute("INSERT INTO checkpoints VALUES (?, ?)", (tid, f"c{i}"))
            conn.execute("INSERT INTO writes VALUES (?, 0)", (tid,))
        conn.execute("INSERT INTO unrelated VALUES ('keep')")
        conn.commit()
    finally:
        conn.close()


def _rows(path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [r[0] for r in conn.execute(f"SELECT thread_id FROM {table}").fetchall()]
    finally:
        conn.close()


def test_thread_id_encoding_is_prefix_addressable():
    assert thread_id_for("s1", 3) == "s1:3"


def test_deletes_only_the_target_session(tmp_path):
    db = tmp_path / "agent_checkpoints.db"
    _make_db(db, ["s1:1", "s1:2", "s10:1", "sx1:1"])
    # checkpoints 2 行 + writes 2 行
    assert delete_session_threads(db, "s1") == 4
    assert sorted(_rows(db, "checkpoints")) == ["s10:1", "sx1:1"]
    assert sorted(_rows(db, "writes")) == ["s10:1", "sx1:1"]


def test_underscore_in_session_id_is_not_a_wildcard(tmp_path):
    db = tmp_path / "agent_checkpoints.db"
    _make_db(db, ["s_1:1", "sx1:1", "s_1:2"])
    # 不转义的话 `LIKE 's_1:%'` 会把 sx1:1 一起删掉
    assert delete_session_threads(db, "s_1") == 4
    assert _rows(db, "checkpoints") == ["sx1:1"]


def test_tables_without_thread_id_are_left_alone(tmp_path):
    db = tmp_path / "agent_checkpoints.db"
    _make_db(db, ["s1:1"])
    delete_session_threads(db, "s1")
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("SELECT COUNT(*) FROM unrelated").fetchone()[0] == 1
    finally:
        conn.close()


def test_missing_db_returns_zero(tmp_path):
    assert delete_session_threads(tmp_path / "nope.db", "s1") == 0


def test_no_matching_rows_returns_zero(tmp_path):
    db = tmp_path / "agent_checkpoints.db"
    _make_db(db, ["other:1"])
    assert delete_session_threads(db, "s1") == 0
    assert _rows(db, "checkpoints") == ["other:1"]


def test_resolve_path_defaults_to_data_dir(tmp_path):
    settings = SimpleNamespace(agent_checkpointer="sqlite", agent_checkpoint_path="", data_dir=str(tmp_path))
    assert resolve_checkpoint_db_path(settings) == tmp_path / "agent_checkpoints.db"


def test_resolve_path_honours_explicit_config(tmp_path):
    explicit = tmp_path / "custom" / "cp.db"
    settings = SimpleNamespace(
        agent_checkpointer="sqlite", agent_checkpoint_path=str(explicit), data_dir=str(tmp_path)
    )
    assert resolve_checkpoint_db_path(settings) == explicit


def test_resolve_path_is_none_when_backend_disabled(tmp_path):
    settings = SimpleNamespace(agent_checkpointer="none", agent_checkpoint_path="", data_dir=str(tmp_path))
    assert resolve_checkpoint_db_path(settings) is None
