"""索引别名（重建不停机切换）的单测。

别名存在的意义：改 chunk 策略 / 换 embedding 模型都要**重建整个索引**。
若直接在生产集合上重建，期间是脏读（新旧混合），且重建失败等于把线上搞坏。
别名把"物理索引"与"逻辑名字"分开 → 切换是原子操作，秒级，且可回滚。
"""
import json

from app.core.errors import InvalidInputError
from app.services.index_alias import POINTER_FILE, IndexAliasManager


class TestAliasManager:
    def test_initialization_is_idempotent(self, tmp_path):
        mgr = IndexAliasManager(tmp_path, base_name="index")
        first = mgr.ensure_initialized()
        second = mgr.ensure_initialized()
        assert first == second
        assert mgr.physical_name() == "index", "首次不得迁移既有索引"
        assert mgr.version() == 1

    def test_pointer_file_is_written(self, tmp_path):
        mgr = IndexAliasManager(tmp_path, base_name="index")
        mgr.ensure_initialized()
        path = tmp_path / POINTER_FILE
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["physical"] == "index"
        assert payload["version"] == 1

    def test_switch_changes_physical_and_version(self, tmp_path):
        mgr = IndexAliasManager(tmp_path, base_name="index")
        mgr.ensure_initialized()
        status = mgr.switch()
        assert status["physical"] == "index_v2"
        assert status["version"] == 2
        assert status["previous"] == "index", "上一版要留着，才能回滚"

    def test_rollback(self, tmp_path):
        mgr = IndexAliasManager(tmp_path, base_name="index")
        mgr.ensure_initialized()
        mgr.switch()
        back = mgr.rollback()
        assert back["physical"] == "index"
        assert back["version"] == 1

    def test_rollback_without_previous_fails(self, tmp_path):
        mgr = IndexAliasManager(tmp_path, base_name="index")
        mgr.ensure_initialized()
        try:
            mgr.rollback()
        except InvalidInputError as exc:
            assert "回滚" in str(exc)
        else:  # pragma: no cover - 不该走到
            raise AssertionError("没有上一版时必须报错")

    def test_switch_to_same_is_noop(self, tmp_path):
        mgr = IndexAliasManager(tmp_path, base_name="index")
        mgr.ensure_initialized()
        status = mgr.switch("index")
        assert status["version"] == 1, "切到同一个物理索引不该自增版本"

    def test_switch_to_explicit_target(self, tmp_path):
        mgr = IndexAliasManager(tmp_path, base_name="kquery_chunks")
        mgr.ensure_initialized()
        status = mgr.switch("kquery_chunks_v7")
        assert status["physical"] == "kquery_chunks_v7"
        assert status["version"] == 2

    def test_no_temp_file_left_behind(self, tmp_path):
        """原子替换：临时文件必须被 replace 掉，不能残留。"""
        mgr = IndexAliasManager(tmp_path, base_name="index")
        mgr.ensure_initialized()
        mgr.switch()
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_corrupted_pointer_is_recovered(self, tmp_path):
        (tmp_path / POINTER_FILE).write_text("{坏掉的 json", encoding="utf-8")
        mgr = IndexAliasManager(tmp_path, base_name="index")
        status = mgr.status()
        assert status["physical"] == "index"
        assert status["version"] == 1

    def test_missing_directory_is_created(self, tmp_path):
        target = tmp_path / "deep" / "nested"
        mgr = IndexAliasManager(target, base_name="index")
        mgr.ensure_initialized()
        assert (target / POINTER_FILE).exists()
