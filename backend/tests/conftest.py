"""测试基座：每个用例独立数据目录 + Mock 模型，无需千问凭据即可跑通全链路。"""
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


# 功能测试默认以 admin 身份访问（鉴权改造后 /admin/* 需 admin 角色）。
# 权限边界类用例应显式传 headers 覆盖 X-Roles（见 test_stage6_agent 的"他人不可读"）。
ADMIN_HEADERS = {"X-User-Id": "admin", "X-Tenant-Id": "default", "X-Roles": "admin"}


@pytest.fixture()
def client():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["MODEL_PROVIDER"] = "mock"
        # 测试走头部透传（HeaderAuthProvider），用例不受凭证约束；生产默认开启见
        os.environ["AUTH_ENABLED"] = "false"
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
        from app.core.config import get_settings

        get_settings.cache_clear()
        from app.main import create_app

        with TestClient(create_app(), headers=ADMIN_HEADERS) as c:
            yield c
        get_settings.cache_clear()
        for key in ("DATA_DIR", "MODEL_PROVIDER", "AUTH_ENABLED", "RETRIEVAL_CONFIG_APPLY_DELAY_S", "VECTOR_STORE", "KQUERY_MILVUS_URI", "CRAG_CORRECT_TH", "CRAG_INCORRECT_TH"):
            os.environ.pop(key, None)


def upload_sample(client: TestClient) -> dict:
    resp = client.post(
        "/knowledge/upload",
        files={"file": ("产品手册.md", SAMPLE_MD.encode("utf-8"), "text/markdown")},
    )
    assert resp.status_code == 200
    return resp.json()["data"]
