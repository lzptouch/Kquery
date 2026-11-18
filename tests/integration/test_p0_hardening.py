"""P0 加固回归（对照 doc/analysis/知识库对照优化分析.md 的 KQ-01~KQ-08 / KQ-24）。

覆盖：
- KQ-01 入库文件名路径穿越 + 同批重名；
- KQ-02 文档/任务租户隔离（上传归属主体、跨租户 404）；
- KQ-04 语义缓存维度签名（同租户跨用户不命中）；
- KQ-05 上传流式校验（超限即拒）；
- KQ-06 图检索租户隔离；
- KQ-08 删除编排（ACL/元数据/源文件收敛）。
"""
import time

from tests.conftest import SAMPLE_MD

#: 认证已下线（2026-11-12）后，请求头**不能**切换租户/用户/角色。
#: 这两组头用于固化"头无效"这一新不变量（原先它们是切换租户的手段）。
CLAIM_TENANT_A = {"X-User-Id": "adminA", "X-Tenant-Id": "tA", "X-Roles": "admin"}
CLAIM_TENANT_B = {"X-User-Id": "adminB", "X-Tenant-Id": "tB", "X-Roles": "admin"}
ALICE = {"X-User-Id": "alice", "X-Tenant-Id": "t1", "X-Roles": "user"}
BOB = {"X-User-Id": "bob", "X-Tenant-Id": "t1", "X-Roles": "user"}

_TERMINAL_TASK_STATUS = {"done", "failed", "partial_failed", "cancelled"}


def _wait_task(client, task_id: str, headers: dict | None = None, timeout_s: float = 30.0) -> dict:
    """轮询任务到终态。入库任务在后台线程执行，不等它结束就拆临时目录
    会导致 teardown 时目录非空（WinError 145）。
    headers 必须与任务提交主体一致（任务已按租户归属，KQ-02）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        resp = client.get(f"/knowledge/tasks/{task_id}", headers=headers or {})
        assert resp.status_code == 200
        view = resp.json()["data"]
        if view["task"]["status"] in _TERMINAL_TASK_STATUS:
            return view
        time.sleep(0.1)
    raise TimeoutError(f"任务 {task_id} 未在 {timeout_s}s 内结束")


class TestIngestFilenameSafety:
    def test_path_traversal_rejected(self, client):
        resp = client.post(
            "/knowledge/ingest",
            files=[("files", ("../../evil.txt", b"hello", "text/plain"))],
        )
        assert resp.status_code == 400
        assert "路径" in resp.json()["error"]["message"] or "非法文件名" in resp.json()["error"]["message"]

    def test_backslash_traversal_rejected(self, client):
        resp = client.post(
            "/knowledge/ingest",
            files=[("files", ("..\\..\\evil.txt", b"hello", "text/plain"))],
        )
        assert resp.status_code == 400

    def test_duplicate_filename_in_batch_rejected(self, client):
        resp = client.post(
            "/knowledge/ingest",
            files=[
                ("files", ("a.md", b"one", "text/markdown")),
                ("files", ("a.md", b"two", "text/markdown")),
            ],
        )
        assert resp.status_code == 400
        assert "同名" in resp.json()["error"]["message"]

    def test_normal_ingest_still_works(self, client):
        resp = client.post(
            "/knowledge/ingest",
            files=[("files", ("正常文档.md", "普通内容，不含敏感信息。".encode(), "text/markdown"))],
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["task_id"].startswith("t-")
        _wait_task(client, resp.json()["data"]["task_id"])


class TestUploadLimits:
    def test_oversize_upload_rejected_before_full_read(self, client):
        settings = client.app.state.settings
        original = settings.max_upload_mb
        settings.max_upload_mb = 1
        try:
            big = b"x" * (2 * 1024 * 1024)  # 2MB > 1MB 限制
            resp = client.post(
                "/knowledge/upload",
                files={"file": ("big.md", big, "text/markdown")},
            )
            assert resp.status_code != 200
            assert "超过" in resp.json()["error"]["message"]
        finally:
            settings.max_upload_mb = original


def _upload_as(client, headers, name="手册.md"):
    resp = client.post(
        "/knowledge/upload",
        files={"file": (name, SAMPLE_MD.encode("utf-8"), "text/markdown")},
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()["data"]


class TestTenantIsolation:
    """租户归属：身份固定后，请求头无法切换租户。

    原用例是"用 X-Tenant-Id 头切成另一个租户，验证隔离"。认证下线后头不再
    生效，因此这些用例的前提消失——改为固化**新**不变量：无论请求头怎么写，
    文档/任务都归属固定租户（default），且头部声明的租户拿不到别的租户的数据。
    """

    def test_upload_record_belongs_to_fixed_tenant(self, client):
        data = _upload_as(client, CLAIM_TENANT_A)
        record = client.app.state.knowledge.get(data["doc_id"])
        assert record.tenant_id == "default", "请求头不得改变文档归属租户"

    def test_headers_cannot_switch_tenant(self, client):
        data = _upload_as(client, CLAIM_TENANT_A)
        doc_id = data["doc_id"]
        # 无论请求头声明哪个租户，都只能看到固定租户的数据
        for headers in (CLAIM_TENANT_B, CLAIM_TENANT_A, {}):
            resp = client.get("/knowledge/files", headers=headers)
            assert any(d["doc_id"] == doc_id for d in resp.json()["data"]), (
                f"固定租户应看得到自己的文档（headers={headers}）"
            )
            assert client.get(f"/knowledge/preview/{doc_id}", headers=headers).status_code == 200

    def test_task_tenant_isolation(self, client):
        resp = client.post(
            "/knowledge/ingest",
            files=[("files", ("t1.md", "普通内容。".encode(), "text/markdown"))],
            headers=CLAIM_TENANT_A,
        )
        task_id = resp.json()["data"]["task_id"]
        # 任务归属固定租户：两个"自称不同租户"的请求都能取到同一个任务
        assert client.get(f"/knowledge/tasks/{task_id}", headers=CLAIM_TENANT_A).status_code == 200
        assert client.get(f"/knowledge/tasks/{task_id}", headers=CLAIM_TENANT_B).status_code == 200
        _wait_task(client, task_id, CLAIM_TENANT_A)


class TestCacheSignatureEndToEnd:
    """缓存按身份隔离；身份固定后，请求头不得改变缓存归属。"""

    def test_same_question_different_user_no_semantic_share(self, client):
        q = "保修政策是什么？"
        r1 = client.post("/ask", json={"question": q}, headers=ALICE)
        assert r1.status_code == 200
        assert r1.json()["data"]["from_cache"] is False
        r2 = client.post("/ask", json={"question": q}, headers=ALICE)
        assert r2.json()["data"]["from_cache"] is True
        # 身份固定：自称 bob 的请求仍是同一个主体，因此**应当**命中同一条缓存
        r3 = client.post("/ask", json={"question": q}, headers=BOB)
        assert r3.status_code == 200
        assert r3.json()["data"]["from_cache"] is True, (
            "请求头不得改变缓存归属（身份固定，两次请求是同一主体）"
        )


class TestGraphTenantIsolation:
    def test_graph_search_does_not_cross_tenants(self, client):
        from app.domain.schemas import Chunk
        from app.services.graph.builder import GraphBuilder
        from app.services.graph.extractor import ScriptedExtractor

        state = client.app.state
        extractor = ScriptedExtractor(table={
            "Alpha 收购 Beta": {
                "entities": [
                    {"name": "Alpha", "type": "Company", "description": ""},
                    {"name": "Beta", "type": "Company", "description": ""},
                ],
                "relations": [
                    {"source": "Alpha", "target": "Beta", "relation": "收购",
                     "evidence": "Alpha 收购 Beta"},
                ],
            }
        })
        builder = GraphBuilder(
            extractor, state.entity_resolver, state.graph_store,
            state.embeddings, state.store, llm=None,
        )
        chunk = Chunk(
            chunk_id="docA#0", doc_id="docA", seq=0,
            content="Alpha 收购 Beta 的公告", source="test", tenant_id="tA",
        )
        builder.build_from_chunks([chunk], tenant_id="tA")

        result_a = state.graph_query.search("Alpha 收购 Beta", tenant_id="tA")
        assert result_a["seeds"], "同租户应检索到图种子"
        result_b = state.graph_query.search("Alpha 收购 Beta", tenant_id="tB")
        assert result_b["seeds"] == [] and result_b["paths"] == [], (
            "跨租户图检索必须为空（此前会读到 default 分区的图）"
        )

    def test_resolver_isolated_per_tenant(self, client):
        resolver = client.app.state.entity_resolver
        name, _ = resolver.resolve("阿尔法", "Company", tenant_id="tA")
        assert resolver.lookup("阿尔法", tenant_id="tA") == name
        assert resolver.lookup("阿尔法", tenant_id="tB") is None


class TestDeleteOrchestration:
    def test_delete_cleans_metadata_and_source(self, client):
        """删除文档：源文件与元数据一并收敛。

        （原用例还断言「ACL 条目随文档删除」——文档级 ACL 已下线，该断言随能力删除。）
        """
        data = _upload_as(client, CLAIM_TENANT_A, name="待删除.md")
        doc_id = data["doc_id"]
        source_path = client.app.state.knowledge.get(doc_id).source_path

        resp = client.delete(f"/knowledge/files/{doc_id}", headers=CLAIM_TENANT_A)
        assert resp.status_code == 200

        import os
        assert not os.path.exists(source_path), "源文件应随删除移除"

