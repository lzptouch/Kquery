"""端到端功能测试：上传 → 索引 → 问答（同步/流式）→ 引用契约 → 删除。

全部走 Mock 模型，验证"答案 + 证据"响应契约与统一信封，
不依赖千问凭据。
"""
import json

from tests.conftest import upload_sample


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "up"


def test_upload_and_list_files(client):
    data = upload_sample(client)
    assert data["status"] == "indexed"
    assert data["chunks"] > 0  # 块数可见：入库成功的直接证据
    assert data["doc_id"] and data["file_id"]

    resp = client.get("/knowledge/files")
    body = resp.json()
    assert body["status"] == "ok"
    files = body["data"]
    assert len(files) == 1
    assert files[0]["filename"] == "产品手册.md"
    assert files[0]["chunks"] == data["chunks"]


def test_upload_unsupported_file(client):
    resp = client.post(
        "/knowledge/upload",
        files={"file": ("notes.txt", b"plain text", "text/plain")},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == 2003


def test_ask_returns_answer_with_evidence(client):
    upload_sample(client)
    resp = client.post("/ask", json={"question": "Kquery 支持哪些文档格式？"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    data = body["data"]

    # answer + evidence 双字段契约
    assert data["answer"]
    assert "[#" in data["answer"]  # 答案带引用编号
    evidence = data["evidence"]
    assert len(evidence) >= 1

    # 编号从 1 起连续，与答案引用标记对应；每条含溯源字段
    assert [e["rank"] for e in evidence] == list(range(1, len(evidence) + 1))
    for ev in evidence:
        assert ev["chunk_id"].startswith(data["evidence"][0]["chunk_id"].split("#")[0] + "#")
        assert ev["content"]
        assert "产品手册.md" in ev["source"]

    assert data["trace_id"]
    assert body["meta"]["trace_id"] == data["trace_id"]


def test_ask_without_knowledge_refuses(client):
    """检索返回 0 条 → 显式拒答（verdict=refused，走成功状态码），不做自由发挥。"""
    resp = client.post("/ask", json={"question": "任意问题"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["verdict"] == "refused"
    assert data["refusal_reason"] == "no_content"
    assert data["confidence"] == 0.0
    assert data["evidence"] == []


def test_ask_rejects_empty_question(client):
    """空问题必须被拒，且响应是统一信封。

    断言不只看状态码：校验错误若走 FastAPI 默认的 {"detail": ...}，
    前端统一读 body.error 就拿不到 message，页面会显示 undefined。
    """
    resp = client.post("/ask", json={"question": ""})
    assert resp.status_code == 400
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] is not None
    assert body["error"]["message"], "错误必须带可照着改的提示文案"


def test_missing_field_returns_envelope_not_detail(client):
    """字段缺失同样走统一信封（422 → 400 契约修正的回归护栏）。"""
    resp = client.post("/ask", json={})
    assert resp.status_code == 400
    body = resp.json()
    assert "detail" not in body, "仍在返回 FastAPI 默认的 detail 结构"
    assert body["error"]["message"]


def test_unknown_path_returns_envelope(client):
    resp = client.get("/no-such-endpoint")
    assert resp.status_code == 404
    assert "detail" not in resp.json()


def test_ask_stream_events(client):
    upload_sample(client)
    with client.stream("POST", "/ask/stream", json={"question": "计费规则是什么？"}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = []
        for line in resp.iter_lines():
            if line.startswith("event: "):
                events.append((line[7:], None))
            elif line.startswith("data: ") and events:
                events[-1] = (events[-1][0], json.loads(line[6:]))

    kinds = [k for k, _ in events]
    # 协议：evidence → token* → done；done 与 error 互斥且必有其一
    assert "evidence" in kinds
    assert "token" in kinds
    assert kinds[-1] == "done"
    assert "error" not in kinds
    done = dict(events)[ "done"]
    assert done["trace_id"] and done["elapsed_ms"] >= 0
    tokens = "".join(d["content"] for k, d in events if k == "token")
    assert "[#" in tokens


def test_delete_file_and_index(client):
    data = upload_sample(client)
    resp = client.delete(f"/knowledge/files/{data['doc_id']}")
    assert resp.json()["status"] == "ok"

    assert client.get("/knowledge/files").json()["data"] == []
    # 删除后检索为空 → 显式拒答
    resp = client.post("/ask", json={"question": "Kquery 支持哪些文档格式？"})
    assert resp.json()["data"]["verdict"] == "refused"


def test_rebuild_keeps_chunk_ids(client):
    data = upload_sample(client)
    before = client.post("/ask", json={"question": "支持哪些文档格式？"}).json()["data"]
    resp = client.post(f"/knowledge/rebuild/{data['doc_id']}")
    assert resp.json()["data"]["status"] == "indexed"
    after = client.post("/ask", json={"question": "支持哪些文档格式？"}).json()["data"]
    # 重建索引后 chunk_id 稳定（缓存键、引用回溯的基石）
    before_ids = sorted(e["chunk_id"] for e in before["evidence"])
    after_ids = sorted(e["chunk_id"] for e in after["evidence"])
    assert before_ids == after_ids


def test_index_persists_across_restart(client, tmp_path):
    """索引落盘：同一数据目录重建应用后直接复用，无需重新上传。"""
    upload_sample(client)
    data_dir = client.app.state.settings.data_dir
    # 释放首个向量库连接（Milvus Lite 单文件不允许并发占用）
    client.app.state.store.close()

    import os

    os.environ["DATA_DIR"] = str(data_dir)
    os.environ["MODEL_PROVIDER"] = "mock"
    from app.core.config import get_settings
    from app.main import create_app
    from fastapi.testclient import TestClient

    get_settings.cache_clear()
    with TestClient(create_app()) as client2:
        files = client2.get("/knowledge/files").json()["data"]
        assert len(files) == 1
        resp = client2.post("/ask", json={"question": "Kquery 是什么？"})
        assert resp.json()["data"]["evidence"]
    get_settings.cache_clear()
    os.environ.pop("DATA_DIR", None)
    os.environ.pop("MODEL_PROVIDER", None)
