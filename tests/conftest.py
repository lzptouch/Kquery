"""测试基座：每个用例独立数据目录 + Mock 模型，无需千问凭据即可跑通全链路。

## 认证与授权已下线（2026-11-12）

原先这里有 `ADMIN_HEADERS`（`X-User-Id` / `X-Tenant-Id` / `X-Roles`）与
`AUTH_ENABLED=false` 环境变量——那套"头部透传"认证档已随注册/登录/口令/JWT/
用户目录一并删除。现在请求身份是**固定值**（tenant=default, user=local），
测试不再需要也不应该传任何身份头。
"""
import contextlib
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

SAMPLE_MD = """# Kquery 产品手册

## 产品简介

Kquery 是一个智能知识库问答系统，支持上传文档后进行基于证据的问答。
它的核心能力包括文档切块、向量检索、引用溯源与流式回答。

## 计费规则

Kquery 按问答次数计费，每次问答消耗一个额度。
企业版每月包含一万次问答额度，超出部分按量付费。

## 常见问题

问：支持哪些文档格式？答：当前支持 PDF 与 Markdown 文档。
问：答案中的引用编号是什么？答：引用编号对应答案所依据的文档片段，可点击跳转查看原文。
"""


@contextlib.contextmanager
def _client_with_env(extra_env: dict[str, str] | None = None):
    """启动一个真实应用（Mock 模型 + 临时数据目录），可追加环境变量覆盖。

    抽成 contextmanager 而不是继续复制 `client` 里的那一大段环境钉死逻辑：
    每多一份副本就多一处"漏钉一个键 → 用例跑在本地配置上"的隐患
    （`CHUNK_SIZE=300` 那次连环红就是这么来的）。
    """
    extra = dict(extra_env or {})
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["MODEL_PROVIDER"] = "mock"
        os.environ["RETRIEVAL_CONFIG_APPLY_DELAY_S"] = "0"  # 测试中配置变更立即生效
        # 向量库：默认 Milvus Lite（内嵌文件库，真实 Milvus API）。Windows 下 milvus-lite
        # 的 gRPC AllocTimestamp 未实现且锁文件 teardown 被占用，回退 FAISS 以便本地跑测；
        # 可用环境变量 VECTOR_STORE 显式覆盖（CI/Linux 仍走真实 Milvus Lite）。
        import sys
        os.environ["VECTOR_STORE"] = os.environ.get("VECTOR_STORE") or (
            "faiss" if sys.platform == "win32" else "milvus"
        )
        if os.environ["VECTOR_STORE"] == "milvus":
            os.environ["KQUERY_MILVUS_URI"] = f"{tmp}/milvus_lite.db"
        # 测试环境的 CRAG 阈值贴合 Mock 评分分布（生产默认 0.7/0.3 须按真实评测集标定）
        os.environ["CRAG_CORRECT_TH"] = "0.3"
        os.environ["CRAG_INCORRECT_TH"] = "0.1"
        # **provider 开关也必须钉死**（2026-11-13）：本地 `backend/.env` 里的
        # `EMBEDDING_PROVIDER` / `RERANK_PROVIDER` / `DEEPSEEK_API_KEY` 会被
        # Settings 读进来——不钉死的话，测试会①真调云厂商（慢、贵、不稳定）
        # ②走错 provider（索引里没有向量 → 检索召回为空 → 一堆用例连环红，
        # 且红的原因与被测代码无关）。
        # 本地配置不得渗进测试。
        os.environ["EMBEDDING_PROVIDER"] = "mock"
        os.environ["RERANK_PROVIDER"] = "mock"
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["DASHSCOPE_API_KEY"] = ""
        # 切块策略也必须钉死（2026-11-13）：`pymilvus` 在 import 时执行
        # `load_dotenv()`，其 `find_dotenv()` 从 `site-packages`（位于
        # `backend/.venv/` 下）向上走，正好命中 `backend/.env`，于是**整份 .env
        # 被注入 `os.environ`**（`CHUNK_SIZE=300` 即由此而来）。`load_dotenv`
        # 不 override，所以只要 conftest 先设好就没问题；漏设的键会让测试
        # 悄悄跑在本地配置上——「默认 800」的用例会被 300 反转。
        os.environ["CHUNK_SIZE"] = "800"
        os.environ["CHUNK_OVERLAP"] = "100"
        os.environ.update(extra)
        from app.core.config import get_settings

        get_settings.cache_clear()
        from app.main import create_app

        with TestClient(create_app()) as c:
            yield c
        get_settings.cache_clear()
        for key in (
            "DATA_DIR", "MODEL_PROVIDER", "RETRIEVAL_CONFIG_APPLY_DELAY_S", "VECTOR_STORE",
            "KQUERY_MILVUS_URI", "CRAG_CORRECT_TH", "CRAG_INCORRECT_TH",
            "EMBEDDING_PROVIDER", "RERANK_PROVIDER", "DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY",
            "CHUNK_SIZE", "CHUNK_OVERLAP",
        ):
            os.environ.pop(key, None)
        for key in extra:
            os.environ.pop(key, None)


@pytest.fixture()
def client():
    with _client_with_env() as c:
        yield c


@pytest.fixture()
def client_with_env():
    """按需追加环境变量启动应用（如 `SSE_RESUME_ENABLED=true`）。"""
    with contextlib.ExitStack() as stack:
        def _make(**env: str) -> TestClient:
            return stack.enter_context(_client_with_env(env))
        yield _make


def upload_sample(client: TestClient) -> dict:
    resp = client.post(
        "/knowledge/upload",
        files={"file": ("产品手册.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200
    return resp.json()["data"]
