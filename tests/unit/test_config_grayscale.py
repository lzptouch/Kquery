"""配置灰度（作用域覆盖 + 稳定分桶）的测试（批次 6）。"""
import pytest

from app.domain.schemas import RetrievalConfig
from app.core.errors import InvalidInputError
from app.services.config_store import RetrievalConfigStore, bucket_of


@pytest.fixture()
def store(tmp_path):
    return RetrievalConfigStore(tmp_path / "retrieval_config.json")


class TestStableBucket:
    def test_same_principal_same_bucket(self):
        """同一用户永远同一桶——否则实验数据无法归因。"""
        assert bucket_of("t1", "u1", 50) == bucket_of("t1", "u1", 50)

    def test_zero_and_hundred(self):
        assert bucket_of("t1", "u1", 0) is False
        assert bucket_of("t1", "u1", 100) is True

    def test_distribution_is_roughly_uniform(self):
        hits = sum(1 for i in range(1000) if bucket_of("t1", f"u{i}", 50))
        assert 400 < hits < 600, f"分桶应接近均匀，实际 {hits}/1000"

    def test_different_principals_can_differ(self):
        # 不是所有人都必须命中，但不应全部相同
        assert len({bucket_of("t1", f"u{i}", 50) for i in range(50)}) == 2


class TestScopedOverrides:
    def test_tenant_scope_wins(self, store):
        store.set_scoped("tenant:t1", RetrievalConfig(rrf_k=40), experiment_id="exp-tenant")
        store.set_scoped("percent:100", RetrievalConfig(rrf_k=30), experiment_id="exp-percent")
        config, label = store.resolve("t1", "u1")
        assert config.rrf_k == 40, "租户定向应优先于分桶"
        assert "exp-tenant" in label

    def test_percent_scope_applies_to_bucketed_users(self, store):
        store.set_scoped("percent:100", RetrievalConfig(rrf_k=30), experiment_id="exp")
        config, label = store.resolve("t9", "u9")
        assert config.rrf_k == 30
        assert "percent" in label

    def test_percent_zero_applies_to_nobody(self, store):
        store.set_scoped("percent:0", RetrievalConfig(rrf_k=30))
        config, label = store.resolve("t9", "u9")
        assert config.rrf_k != 30
        assert label == ""

    def test_disabled_scope_is_ignored(self, store):
        store.set_scoped("tenant:t1", RetrievalConfig(rrf_k=40), enabled=False)
        config, label = store.resolve("t1", "u1")
        assert config.rrf_k != 40
        assert label == ""

    def test_global_fallback(self, store):
        config, label = store.resolve("t1", "u1")
        assert label == ""
        assert config is not None

    def test_clear_scope(self, store):
        store.set_scoped("tenant:t1", RetrievalConfig(rrf_k=40))
        assert store.clear_scoped("tenant:t1") is True
        assert store.clear_scoped("tenant:t1") is False
        config, _ = store.resolve("t1", "u1")
        assert config.rrf_k != 40

    def test_scoped_view_lists_entries(self, store):
        store.set_scoped("tenant:t1", RetrievalConfig(rrf_k=40), experiment_id="e1")
        view = store.scoped_view()
        assert view and view[0]["scope"] == "tenant:t1"
        assert view[0]["experiment_id"] == "e1"

    def test_invalid_scope_rejected(self, store):
        with pytest.raises(InvalidInputError):
            store.set_scoped("", RetrievalConfig())
        with pytest.raises(InvalidInputError):
            store.set_scoped("tenant:", RetrievalConfig())
        with pytest.raises(InvalidInputError):
            store.set_scoped("percent:200", RetrievalConfig())

    def test_scoped_survives_restart(self, tmp_path):
        """灰度配置要持久化：重启后灰度不能"人间蒸发"。"""
        path = tmp_path / "cfg.json"
        first = RetrievalConfigStore(path)
        first.set_scoped("tenant:t1", RetrievalConfig(rrf_k=40), experiment_id="e1")
        second = RetrievalConfigStore(path)
        config, label = second.resolve("t1", "u1")
        assert config.rrf_k == 40
        assert "e1" in label
