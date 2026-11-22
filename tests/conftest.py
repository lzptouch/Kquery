"""测试基座：每个用例独立数据目录 + Mock 模型，无需千问凭据即可跑通全链路。

## 认证与授权已下线

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


#: 测试连接的 Milvus 服务地址。默认标准端口；本机 19530 被别的容器占用时，
#: 用 `KQUERY_MILVUS_URI=http://localhost:19531 pytest` 指定（deploy/docker 暴露 19531）。
#: 在 import pymilvus **之前**读取，避免 pymilvus 的 `load_dotenv()` 把 backend/.env
#: 里的地址悄悄注入进来——测试地址必须可复现。
MILVUS_TEST_URI = os.environ.get("KQUERY_MILVUS_URI") or "http://localhost:19530"

# 同一个泄漏通道的 OCR_PROVIDER 版本：pymilvus import 时 `load_dotenv()` 会把
# backend/.env 整体注进 `os.environ`（find_dotenv 从 site-packages 向上命中
# backend/.env，且不 override 已有键）。OCR_PROVIDER 是本地运行档配置（可能是
# zhipu/dashscope 真云），必须在任何 pymilvus import 之前钉成 mock，
# 否则 TestOcrConfig 这类直接构造 Settings 的单元用例会读到本地值。
os.environ.setdefault("OCR_PROVIDER", "mock")
#: 测试专用集合：所有用例共用一个集合，靠"用例开始前清空"做隔离。
#: 不用"每个用例一个集合"是因为 Milvus 建带索引的集合要 1 秒以上（同步等索引构建），
#: 2000 例就是半小时；共享集合 + delete 清空约 0.2 秒。
MILVUS_TEST_COLLECTION = "kquery_test"

_milvus_admin = None
_session_bootstrapped = False


def _milvus():
    """懒加载的 Milvus 管理客户端（清空/删除测试集合）。"""
    global _milvus_admin
    if _milvus_admin is None:
        from pymilvus import MilvusClient

        _milvus_admin = MilvusClient(uri=MILVUS_TEST_URI)
    return _milvus_admin


def reset_vector_store() -> None:
    """清空测试集合（集合不存在时留给应用启动时创建）。"""
    global _session_bootstrapped
    client = _milvus()
    try:
        if not _session_bootstrapped:
            # 会话起点直接 drop：上一个会话（尤其被中断的）留下的集合带着
            # 旧 schema / 旧一致性级别 / 大量 delete tombstone，delete 清不掉
            # 这些元信息。drop 一次约 0.1s，由应用启动时按当前代码重建。
            if client.has_collection(MILVUS_TEST_COLLECTION):
                client.drop_collection(MILVUS_TEST_COLLECTION)
            _session_bootstrapped = True
            return
        if client.has_collection(MILVUS_TEST_COLLECTION):
            client.load_collection(MILVUS_TEST_COLLECTION)  # delete 要求集合已加载
            # seq 是必填 INT64，对所有行恒成立，等价于「清空全部」
            client.delete(MILVUS_TEST_COLLECTION, filter="seq >= 0")
    except Exception as exc:  # noqa: BLE001 - 连不上要立刻喊，不能静默降级到别的库
        pytest.exit(
            f"测试需要可用的 Milvus 服务（当前地址 {MILVUS_TEST_URI}）：{exc}\n"
            "起法：cd deploy/docker && docker compose up -d etcd milvus",
            returncode=1,
        )


def configure_vector_backend() -> None:
    """钉死向量库配置：真实 Milvus 服务 + 测试集合 + Strong 一致性。

    Milvus Lite（pymilvus 内嵌单文件）形态已下线：生产与测试连
    同一个服务，否则「本地 lite 通过、上线服务端行为不同」只会在部署后暴露。
    `VECTOR_STORE=faiss` 仍可显式覆盖（少数用例只关心本地索引路径）。
    """
    os.environ["VECTOR_STORE"] = os.environ.get("VECTOR_STORE") or "milvus"
    if os.environ["VECTOR_STORE"] != "milvus":
        return
    os.environ["KQUERY_MILVUS_URI"] = MILVUS_TEST_URI
    os.environ["MILVUS_COLLECTION"] = MILVUS_TEST_COLLECTION
    # Strong：写完立刻可查。用例共享集合，依赖它保证上一个用例的数据清空后
    # 不会残留在下一次查询里（Bounded 下 delete 到可见有毫秒级窗口）。
    os.environ["MILVUS_CONSISTENCY_LEVEL"] = "Strong"
    reset_vector_store()


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest 固定签名
    """收尾：删掉测试集合并断开连接，避免在 Milvus 里留垃圾集合。"""
    global _milvus_admin
    if _milvus_admin is None:
        return
    try:
        if _milvus_admin.has_collection(MILVUS_TEST_COLLECTION):
            _milvus_admin.drop_collection(MILVUS_TEST_COLLECTION)
    except Exception:  # noqa: BLE001 - 收尾失败不该影响测试结论
        pass
    finally:
        _milvus_admin.close()
        _milvus_admin = None


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
        # 测试环境的 CRAG 阈值贴合 Mock 评分分布（生产默认 0.7/0.3 须按真实评测集标定）
        os.environ["CRAG_CORRECT_TH"] = "0.3"
        os.environ["CRAG_INCORRECT_TH"] = "0.1"
        # **provider 开关也必须钉死**：本地 `backend/.env` 里的
        # `EMBEDDING_PROVIDER` / `RERANK_PROVIDER` / `DEEPSEEK_API_KEY` 会被
        # Settings 读进来——不钉死的话，测试会①真调云厂商（慢、贵、不稳定）
        # ②走错 provider（索引里没有向量 → 检索召回为空 → 一堆用例连环红，
        # 且红的原因与被测代码无关）。
        # 本地配置不得渗进测试。
        os.environ["EMBEDDING_PROVIDER"] = "mock"
        os.environ["RERANK_PROVIDER"] = "mock"
        os.environ["DEEPSEEK_API_KEY"] = ""
        os.environ["DASHSCOPE_API_KEY"] = ""
        # 切块策略也必须钉死：`pymilvus` 在 import 时执行
        # `load_dotenv()`，其 `find_dotenv()` 从 `site-packages`（位于
        # `backend/.venv/` 下）向上走，正好命中 `backend/.env`，于是**整份 .env
        # 被注入 `os.environ`**（`CHUNK_SIZE=300` 即由此而来）。`load_dotenv`
        # 不 override，所以只要 conftest 先设好就没问题；漏设的键会让测试
        # 悄悄跑在本地配置上——「默认 800」的用例会被 300 反转。
        os.environ["CHUNK_SIZE"] = "800"
        os.environ["CHUNK_OVERLAP"] = "100"
        os.environ.update(extra)
        # 向量库在 extra 之后再钉：用例可以用 extra 把 VECTOR_STORE 换成 faiss，
        # 其余情况统一连真实 Milvus 服务，并把测试集合清空（用例间隔离）。
        configure_vector_backend()
        from app.core.config import get_settings

        get_settings.cache_clear()
        from app.main import create_app

        with TestClient(create_app()) as c:
            yield c
        get_settings.cache_clear()
        for key in (
            "DATA_DIR", "MODEL_PROVIDER", "RETRIEVAL_CONFIG_APPLY_DELAY_S", "VECTOR_STORE",
            "KQUERY_MILVUS_URI", "MILVUS_COLLECTION", "MILVUS_CONSISTENCY_LEVEL",
            "CRAG_CORRECT_TH", "CRAG_INCORRECT_TH",
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
