"""TASK-180：SQLite 图存储。

核心验收不是"能存取"，而是**与内存版契约等价**——已有 20+ 个测试与
GraphBuilder / GraphQuery / CommunityDetector 三个消费者都建立在既有语义上，
新实现任何一处语义偏差都会静默改变图谱行为。
"""
import json

import pytest

from app.domain.schemas import Community, EntityNode, RelationEdge


def _entity(name, tenant="t1", **kw):
    return EntityNode(entity_id=f"{tenant}:{name}", name=name, tenant_id=tenant, **kw)


def _rel(source, relation, target, tenant="t1", chunks=("c1",)):
    return RelationEdge(source=source, target=target, relation=relation,
                        tenant_id=tenant, source_chunks=list(chunks))


@pytest.fixture()
def stores(tmp_path):
    """同时提供内存版与 SQLite 版，供契约一致性对比。"""
    from app.services.graph.sqlite_store import SqliteGraphStore
    from app.services.graph.store import InMemoryGraphStore

    return InMemoryGraphStore(tmp_path / "mem.json"), SqliteGraphStore(tmp_path / "g.db")


class TestContractParity:
    """两个实现必须给出相同的可见行为。"""

    def _seed(self, store):
        for n in ("甲", "乙", "丙"):
            store.upsert_entity(_entity(n))
        store.upsert_relation(_rel("甲", "投资", "乙"))
        store.upsert_relation(_rel("乙", "合作", "丙"))
        store.set_community(Community(community_id="c1", tenant_id="t1", level=0,
                                      members=["甲", "乙"], summary="摘要"))

    def test_stats_shape_identical(self, stores):
        mem, sql = stores
        self._seed(mem)
        self._seed(sql)
        assert mem.stats("t1") == sql.stats("t1")
        assert set(mem.stats("t1")) == set(sql.stats("t1"))

    def test_all_entities_identical(self, stores):
        mem, sql = stores
        self._seed(mem)
        self._seed(sql)
        assert {e.name for e in mem.all_entities("t1")} == {e.name for e in sql.all_entities("t1")}

    def test_neighbors_identical(self, stores):
        mem, sql = stores
        self._seed(mem)
        self._seed(sql)
        m = [(x["from"], x["relation"], x["to"], x["direction"]) for x in mem.neighbors("甲", 2, tenant_id="t1")]
        s = [(x["from"], x["relation"], x["to"], x["direction"]) for x in sql.neighbors("甲", 2, tenant_id="t1")]
        assert m == s

    def test_get_entity_identical(self, stores):
        mem, sql = stores
        self._seed(mem)
        self._seed(sql)
        assert mem.get_entity("甲", "t1").name == sql.get_entity("甲", "t1").name
        assert mem.get_entity("不存在", "t1") is None
        assert sql.get_entity("不存在", "t1") is None

    def test_tenant_isolation_identical(self, stores):
        mem, sql = stores
        for s in (mem, sql):
            s.upsert_entity(_entity("同名", "t1"))
            s.upsert_entity(_entity("同名", "t2"))
            s.upsert_relation(_rel("同名", "关联", "x", "t1"))
            s.upsert_relation(_rel("同名", "关联", "y", "t2"))
        for s in (mem, sql):
            assert len(s.all_entities()) == 2, "同名不同租户不得互相覆盖"
            assert s.neighbors("同名", 1, tenant_id="t1")[0]["to"] == "x"
            assert s.neighbors("同名", 1, tenant_id="t2")[0]["to"] == "y"


class TestIdempotentUpsert:
    def test_entity_merge_keeps_description_and_unions_aliases(self, stores):
        mem, sql = stores
        for s in (mem, sql):
            s.upsert_entity(_entity("甲", description="最初的描述"))
            s.upsert_entity(_entity("甲", description="后来的描述", aliases=["甲甲"]))
            e = s.get_entity("甲", "t1")
            assert e.description == "最初的描述", "已有描述不应被覆盖"
            assert "甲甲" in e.aliases

    def test_relation_weight_accumulates(self, stores):
        """同关系多次写入 → 强度累加并封顶（多条证据应更可信）。"""
        mem, sql = stores
        for s in (mem, sql):
            s.upsert_entity(_entity("甲"))
            s.upsert_entity(_entity("乙"))
            s.upsert_relation(_rel("甲", "投资", "乙", chunks=["c1"]))
            s.upsert_relation(_rel("甲", "投资", "乙", chunks=["c2"]))
            rels = s.all_relations("t1")
            assert len(rels) == 1, "同关系应合并而非追加"
            assert set(rels[0].source_chunks) == {"c1", "c2"}
            assert rels[0].weight > 1.0

    def test_degree_updated_on_relation(self, stores):
        mem, sql = stores
        for s in (mem, sql):
            s.upsert_entity(_entity("甲"))
            s.upsert_entity(_entity("乙"))
            s.upsert_relation(_rel("甲", "投资", "乙"))
            assert s.get_entity("甲", "t1").degree == 1
            assert s.get_entity("乙", "t1").degree == 1


class TestDeletion:
    def test_delete_removes_relation_and_orphan(self, stores):
        mem, sql = stores
        for s in (mem, sql):
            s.upsert_entity(_entity("甲"))
            s.upsert_entity(_entity("乙"))
            s.upsert_relation(_rel("甲", "投资", "乙", chunks=["c1"]))
            removed = s.delete_doc_chunks({"c1"})
            assert removed == 1
            assert s.all_relations("t1") == []
            assert s.all_entities("t1") == [], "孤立实体应被清理"

    def test_partial_delete_keeps_relation(self, stores):
        """一个关系由多个块支撑时，只摘掉该块而不是删关系。"""
        mem, sql = stores
        for s in (mem, sql):
            s.upsert_entity(_entity("甲"))
            s.upsert_entity(_entity("乙"))
            s.upsert_relation(_rel("甲", "投资", "乙", chunks=["c1", "c2"]))
            removed = s.delete_doc_chunks({"c1"})
            assert removed == 0
            rels = s.all_relations("t1")
            assert len(rels) == 1 and rels[0].source_chunks == ["c2"]

    def test_entity_in_community_survives_orphan_cleanup(self, stores):
        """仍被社区摘要引用的实体不因孤立而删除（摘要是独立产物）。"""
        mem, sql = stores
        for s in (mem, sql):
            s.upsert_entity(_entity("甲"))
            s.upsert_entity(_entity("乙"))
            s.upsert_relation(_rel("甲", "投资", "乙", chunks=["c1"]))
            s.set_community(Community(community_id="c1", tenant_id="t1", level=0,
                                      members=["乙"], summary="s"))
            s.delete_doc_chunks({"c1"})
            assert {e.name for e in s.all_entities("t1")} == {"乙"}


class TestPersistence:
    def test_survives_reopen(self, tmp_path):
        """重启后可读（SQLite 的核心价值：崩溃不会像 JSON 全量重写那样丢全库）。"""
        from app.services.graph.sqlite_store import SqliteGraphStore

        path = tmp_path / "g.db"
        s1 = SqliteGraphStore(path)
        s1.upsert_entity(_entity("甲"))
        s1.upsert_entity(_entity("乙"))
        s1.upsert_relation(_rel("甲", "投资", "乙"))
        s1.close()

        s2 = SqliteGraphStore(path)
        assert {e.name for e in s2.all_entities("t1")} == {"甲", "乙"}
        assert len(s2.all_relations("t1")) == 1
        s2.close()

    def test_save_is_noop(self, tmp_path):
        """save() 是空操作——数据在写操作时已提交（不会"忘记保存"）。"""
        from app.services.graph.sqlite_store import SqliteGraphStore

        s = SqliteGraphStore(tmp_path / "g.db")
        s.upsert_entity(_entity("甲"))
        s.save()
        assert s.get_entity("甲", "t1") is not None
        s.close()

    def test_incremental_write_does_not_rewrite_all(self, tmp_path):
        """增量写入：新增 1 个实体后，已有实体的行**不被重写**。

        这是相比 JSON 全量落盘的实质改进：用 rowid 变化证明只有新行被插入。
        """
        import sqlite3

        from app.services.graph.sqlite_store import SqliteGraphStore

        path = tmp_path / "g.db"
        s = SqliteGraphStore(path)
        for i in range(50):
            s.upsert_entity(_entity(f"e{i}"))
        conn = sqlite3.connect(path)
        before = dict(conn.execute("SELECT name, rowid FROM entities").fetchall())
        conn.close()

        s.upsert_entity(_entity("new"))
        conn = sqlite3.connect(path)
        after = dict(conn.execute("SELECT name, rowid FROM entities").fetchall())
        conn.close()

        for name, rid in before.items():
            assert after[name] == rid, f"{name} 的行被重写了（退化成全量重写）"
        assert len(after) == 51
        s.close()


class TestMigration:
    def test_json_is_migrated_and_backed_up(self, tmp_path):
        """升级路径：旧 JSON 图谱自动导入 SQLite 并改名备份（不删除原数据）。"""
        from app.services.graph.sqlite_store import SqliteGraphStore, _migrate_json_to_sqlite

        json_path = tmp_path / "graph.json"
        json_path.write_text(json.dumps({
            "entities": [_entity("甲").model_dump(), _entity("乙").model_dump()],
            "relations": [_rel("甲", "投资", "乙").model_dump()],
            "communities": [],
        }, ensure_ascii=False), encoding="utf-8")

        store = SqliteGraphStore(tmp_path / "graph.db")
        _migrate_json_to_sqlite(json_path, store)

        assert {e.name for e in store.all_entities("t1")} == {"甲", "乙"}
        assert not json_path.exists(), "原 JSON 应被改名"
        assert json_path.with_suffix(".json.migrated").exists(), "必须保留备份（不删除用户数据）"
        store.close()

    def test_corrupt_json_does_not_break_startup(self, tmp_path):
        """损坏的旧文件不应阻断启动（迁移失败 → 用空库继续跑）。"""
        from app.services.graph.sqlite_store import SqliteGraphStore, _migrate_json_to_sqlite

        json_path = tmp_path / "graph.json"
        json_path.write_text("{ 这不是合法 JSON", encoding="utf-8")
        store = SqliteGraphStore(tmp_path / "graph.db")
        _migrate_json_to_sqlite(json_path, store)  # 不应抛
        assert store.all_entities("t1") == []
        store.close()


class TestBuildGraphStore:
    def test_backend_selection(self, tmp_path):
        from app.core.config import Settings
        from app.services.graph.sqlite_store import SqliteGraphStore, build_graph_store
        from app.services.graph.store import InMemoryGraphStore

        assert isinstance(
            build_graph_store(Settings(graph_store_backend="sqlite"), tmp_path),
            SqliteGraphStore,
        )
        assert isinstance(
            build_graph_store(Settings(graph_store_backend="memory"), tmp_path),
            InMemoryGraphStore,
        )

    def test_graph_path_override(self, tmp_path):
        from app.core.config import Settings
        from app.services.graph.sqlite_store import SqliteGraphStore, build_graph_store

        custom = tmp_path / "custom" / "my.json"
        store = build_graph_store(
            Settings(graph_store_backend="sqlite", graph_path=str(custom)), tmp_path
        )
        assert isinstance(store, SqliteGraphStore)
        assert (tmp_path / "custom" / "my.db").exists()
        store.close()
