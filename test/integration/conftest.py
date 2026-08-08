"""集成测试层 conftest：启动 docker-compose.test.yml 三件套并初始化测试数据。"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.5)
    return False


@pytest.fixture(scope="session", autouse=True)
def ensure_test_infrastructure() -> None:
    """检查 Redis/Mongo/Neo4j 是否可用；不可用则 skip 整个集成测试会话。"""
    redis_ok = _wait_for_port("localhost", 26379)
    mongo_ok = _wait_for_port("localhost", 27027)
    neo4j_ok = _wait_for_port("localhost", 27687)

    missing = []
    if not redis_ok:
        missing.append("Redis:26379")
    if not mongo_ok:
        missing.append("MongoDB:27027")
    if not neo4j_ok:
        missing.append("Neo4j:27687")

    if missing:
        pytest.skip(f"测试基础设施未就绪: {', '.join(missing)}；请先运行 docker compose -f docker-compose.test.yml up -d")


@pytest.fixture(scope="session")
def test_env() -> dict:
    """返回测试环境变量。"""
    return {
        "REDIS_URL": os.environ.get("REDIS_URL", "redis://localhost:26379/0"),
        "MONGO_URI": os.environ.get("MONGO_URI", "mongodb://localhost:27027/kquery_test"),
        "NEO4J_URI": os.environ.get("NEO4J_URI", "bolt://localhost:27687"),
        "NEO4J_USER": os.environ.get("NEO4J_USER", "neo4j"),
        "NEO4J_PASSWORD": os.environ.get("NEO4J_PASSWORD", "SecureNeo4j@2026"),
    }


@pytest.fixture(scope="session")
def sample_doc_dir() -> Path:
    """返回 sample_documents 目录路径。"""
    return Path(__file__).resolve().parents[2] / "sample_documents"


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """集成测试中同样避免真实 LLM 调用。"""
    with patch("app.core.llm_client.call_openai_chat") as mock_openai, \
         patch("app.core.llm_client.call_dashscope") as mock_dashscope, \
         patch("app.core.llm_client.is_llm_configured") as mock_configured:
        mock_openai.return_value = {"role": "assistant", "content": "mock answer"}
        mock_dashscope.return_value = {"role": "assistant", "content": "mock answer"}
        mock_configured.return_value = False
        yield {
            "call_openai_chat": mock_openai,
            "call_dashscope": mock_dashscope,
            "is_llm_configured": mock_configured,
        }


@pytest.fixture(scope="session")
def test_tenant_id() -> str:
    """返回固定测试租户 ID。"""
    return "tenant_test"


@pytest.fixture(scope="session")
def test_collection_name() -> str:
    return "kquery_test_collection"


@pytest.fixture(autouse=True)
async def cleanup_after_test() -> AsyncGenerator[None, None]:
    """每个集成测试用例后清理测试 Redis/Mongo/Neo4j 数据。"""
    yield
    # Redis: 选择测试 db 并 flush
    try:
        import redis.asyncio as redis

        r = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:26379/0"))
        await r.flushdb()
        await r.close()
    except Exception:
        pass

    # Mongo: 删除测试集合
    try:
        import motor.motor_asyncio

        mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27027/kquery_test")
        client = motor.motor_asyncio.AsyncIOMotorClient(mongo_uri)
        db_name = mongo_uri.rsplit("/", 1)[-1]
        await client.drop_database(db_name)
        client.close()
    except Exception:
        pass

    # Neo4j: 清理测试图（可选：删除所有带测试标签的节点/关系）
    try:
        from neo4j import AsyncGraphDatabase

        uri = os.environ.get("NEO4J_URI", "bolt://localhost:27687")
        user = os.environ.get("NEO4J_USER", "neo4j")
        password = os.environ.get("NEO4J_PASSWORD", "SecureNeo4j@2026")
        driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
        async with driver.session() as session:
            await session.run("MATCH (n:Test) DETACH DELETE n")
        await driver.close()
    except Exception:
        pass
