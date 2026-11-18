"""真相源损坏必须拒绝启动（`A-11`）。

## 纪律

「真相源损坏即拒绝启动」是全局纪律。损坏的图谱若「从空图启动」，等于**静默丢数据**：
用户以为图谱还在，实际已被清空，而且没有任何错误提示——这类故障比直接崩溃更难查。

## 覆盖三处存储

| 存储 | 此前行为 | 现在 |
| --- | --- | --- |
| 图谱（JSON 内存后端） | 记日志后「从空图启动」 | 抛 `StorageCorruptedError` |
| 图谱（SQLite 后端） | 裸 `DatabaseError` 冒泡 | 抛 `StorageCorruptedError`（带错误码 + 指引） |
| 文档元数据 | 裸 `RuntimeError` | 抛 `StorageCorruptedError`（**与图谱同码**） |
"""
from __future__ import annotations

import json

import pytest

from app.core.envelope import ErrCode
from app.core.errors import StorageCorruptedError


def test_memory_backend_refuses_to_start_empty(tmp_path):
    """内存（JSON）后端：快照损坏 → 显式拒绝，不静默清空。"""
    from app.services.graph.store import InMemoryGraphStore

    path = tmp_path / "graph.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")

    with pytest.raises(StorageCorruptedError) as excinfo:
        InMemoryGraphStore(path)
    err = excinfo.value
    assert int(err.code) == int(ErrCode.STORAGE_CORRUPTED)
    assert "损坏" in err.message
    # 修复指引必须给出来（否则值班只能自己猜）
    assert "重建" in err.message or "备份" in err.message
    # 路径不进 message（message 可能出现在 HTTP 响应体里）
    assert str(path) not in err.message
    assert err.path == path


def test_sqlite_backend_raises_unified_error(tmp_path):
    """SQLite 后端：文件损坏 → 统一异常（带错误码 + 指引），不是裸 DatabaseError。"""
    from app.services.graph.sqlite_store import SqliteGraphStore

    db = tmp_path / "graph.db"
    db.write_bytes(b"this is definitely not a sqlite database, at all")

    with pytest.raises(StorageCorruptedError) as excinfo:
        SqliteGraphStore(db)
    err = excinfo.value
    assert int(err.code) == int(ErrCode.STORAGE_CORRUPTED)
    assert "损坏" in err.message
    assert "重建" in err.message or "备份" in err.message
    assert str(db) not in err.message
    assert err.path == db


def test_sqlite_backend_error_is_not_bare_database_error(tmp_path):
    """回归护栏：不能退回裸 `sqlite3.DatabaseError`（没有错误码就没法分流）。"""
    from app.services.graph.sqlite_store import SqliteGraphStore

    db = tmp_path / "broken.db"
    db.write_bytes(b"garbage garbage garbage")

    with pytest.raises(StorageCorruptedError):
        SqliteGraphStore(db)


def test_metadata_store_uses_same_error_code(tmp_path):
    """文档元数据损坏与图谱损坏**同码**（同一类信号，运维只看一个码）。"""
    from app.core.config import Settings
    from app.services.knowledge import KnowledgeService

    settings = Settings(_env_file=None)
    settings.data_dir = tmp_path
    metadata = tmp_path / "metadata.json"
    metadata.write_text("{ broken", encoding="utf-8")

    service = KnowledgeService.__new__(KnowledgeService)  # 只测加载路径，不做全套装配
    service._settings = settings
    service._docs = {}

    with pytest.raises(StorageCorruptedError) as excinfo:
        service._load_metadata()
    assert int(excinfo.value.code) == int(ErrCode.STORAGE_CORRUPTED)
    assert metadata.exists()


def test_healthy_snapshot_still_loads(tmp_path):
    """回归护栏：正常快照必须照常加载（别把 fail-fast 变成「总是失败」）。"""
    from app.services.graph.store import InMemoryGraphStore

    path = tmp_path / "graph.json"
    path.write_text(
        json.dumps({"entities": [], "relations": [], "communities": []}), encoding="utf-8"
    )
    store = InMemoryGraphStore(path)
    assert store.stats()["entities"] == 0
