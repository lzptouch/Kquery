"""存量升级迁移：无 knowledge_base_id 的文档回填默认 KB。

要解决的问题：升级前的 metadata.json 里没有 `knowledge_base_id` 字段。
若不回填，这些文档的 KB 归属为空，KB 判定会落到"空串归一化"兜底路径——
一旦有人改动兜底逻辑，它们就会集体失访（升级即事故）。

因此本文件用**两次 app 启动**模拟真实升级：先上传建档，再把字段从
metadata.json 里删掉（还原成存量形态），重启后验证回填与"行为不变"。
"""
import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.conftest import ADMIN_HEADERS, SAMPLE_MD

_ENV_KEYS = (
    "DATA_DIR", "MODEL_PROVIDER", "AUTH_ENABLED", "VECTOR_STORE",
    "RETRIEVAL_CONFIG_APPLY_DELAY_S", "SPEECH_ASR_PROVIDER",
)


def _boot(data_dir: str):
    """按 conftest 口径起一个 app（独立管理环境变量，不复用 client fixture——
    本用例需要**两个** app 实例共享同一个 DATA_DIR 才能模拟升级）。"""
    os.environ["DATA_DIR"] = data_dir
    os.environ["MODEL_PROVIDER"] = "mock"
    os.environ["AUTH_ENABLED"] = "false"
    os.environ["VECTOR_STORE"] = "faiss"
    os.environ["RETRIEVAL_CONFIG_APPLY_DELAY_S"] = "0"
    os.environ["SPEECH_ASR_PROVIDER"] = "none"
    get_settings.cache_clear()
    from app.main import create_app

    return create_app()


def test_existing_doc_backfilled_to_default_kb_and_still_searchable():
    with tempfile.TemporaryDirectory() as tmp:
        # ① 旧版本：上传一份文档（建档 + 索引）
        with TestClient(_boot(tmp), headers=ADMIN_HEADERS) as c:
            resp = c.post(
                "/knowledge/upload",
                files={"file": ("手册.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
            )
            assert resp.status_code == 200, resp.json()
            doc_id = resp.json()["data"]["doc_id"]
            assert resp.json()["data"]["knowledge_base_id"] == "default"

        # ② 还原成存量形态：metadata.json 里没有 knowledge_base_id 字段
        meta_path = Path(tmp) / "metadata.json"
        records = json.loads(meta_path.read_text(encoding="utf-8"))
        assert records and "knowledge_base_id" in records[0]
        for r in records:
            r.pop("knowledge_base_id", None)
        meta_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # ③ 升级后首次启动：应回填并落盘
        with TestClient(_boot(tmp), headers=ADMIN_HEADERS) as c2:
            record = c2.app.state.knowledge.get(doc_id)
            assert record.knowledge_base_id == "default", (
                "存量文档未回填到默认 KB（升级即失访）"
            )
            saved = json.loads(meta_path.read_text(encoding="utf-8"))
            assert all(r.get("knowledge_base_id") == "default" for r in saved), (
                "回填结果未落盘（每次启动都要重算，且下次读仍是无归属状态）"
            )

            # ④ 行为不变：升级前的检索路径仍然命中
            data = c2.post(
                "/ask", json={"question": "支持哪些文档格式？", "mode": "auto"}
            ).json()["data"]
            assert data["evidence"], "升级后存量文档必须仍可检索（行为不得回归）"

    get_settings.cache_clear()
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def test_default_kb_created_on_upgrade_with_existing_documents():
    """升级场景：data 目录已有文档但没有 knowledge_bases.json → 默认 KB 自动补建。"""
    with tempfile.TemporaryDirectory() as tmp:
        with TestClient(_boot(tmp), headers=ADMIN_HEADERS) as c:
            c.post(
                "/knowledge/upload",
                files={"file": ("手册.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
            )
        # 模拟只有文档、没有 KB 文件的存量状态
        kb_file = Path(tmp) / "knowledge_bases.json"
        assert kb_file.exists(), "首次启动即应创建 KB 文件"
        kb_file.unlink()

        with TestClient(_boot(tmp), headers=ADMIN_HEADERS) as c2:
            kbs = c2.get("/admin/kb").json()["data"]
            default = next((k for k in kbs if k["kb_id"] == "default"), None)
            assert default is not None, "默认 KB 未自动补建（存量文档将无处归属）"
            assert default["is_public"] is True, "默认 KB 必须公开（存量文档升级后仍全员可见）"
            assert default["doc_count"] >= 1, "默认 KB 未统计到存量文档"

    get_settings.cache_clear()
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
