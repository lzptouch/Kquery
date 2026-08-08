"""单元测试层共享 fixtures：所有外部依赖（Redis/Milvus/Neo4j/LLM）均在此 mock。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dotenv import load_dotenv

# 将 backend 加入路径，确保可以 import app
BACKEND_DIR = Path(__file__).resolve().parents[3] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 加载测试环境变量（不配置 LLM Key，保证内置 Mock 降级）
ENV_FILE = Path(__file__).resolve().parents[2] / ".env.test"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE, override=True)


@pytest.fixture(autouse=True)
def _preserve_llm_config() -> None:
    """保留 .env.test 中的 LLM 配置，允许单元测试使用真实模型（若已配置）。"""
    # .env.test 已加载 LLM_AGENT_API_KEY / LLM_EMBEDDING_API_KEY 等。
    # 此处不再清空环境变量，使 is_llm_configured() 能返回 True。
    pass


@pytest.fixture
def mock_redis() -> MagicMock:
    """返回一个已配置常用方法的 MagicMock Redis 客户端。"""
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.exists = AsyncMock(return_value=0)
    redis.hget = AsyncMock(return_value=None)
    redis.hset = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    redis.pipeline = MagicMock(return_value=redis)
    return redis


@pytest.fixture
def mock_milvus_client() -> MagicMock:
    """返回一个 mock Milvus 客户端。"""
    client = MagicMock()
    client.search = MagicMock(return_value=[])
    client.insert = MagicMock(return_value=[])
    client.has_collection = MagicMock(return_value=False)
    client.create_collection = MagicMock(return_value=True)
    client.load_collection = MagicMock(return_value=True)
    client.drop_collection = MagicMock(return_value=True)
    return client


@pytest.fixture
def mock_neo4j_driver() -> MagicMock:
    """返回一个 mock Neo4j driver。"""
    driver = MagicMock()
    session = MagicMock()
    tx = MagicMock()
    tx.run = MagicMock(return_value=MagicMock(data=MagicMock(return_value=[])))
    session.__enter__ = MagicMock(return_value=session)
    session.__exit__ = MagicMock(return_value=False)
    session.execute_read = AsyncMock(return_value=[])
    session.execute_write = AsyncMock(return_value=[])
    driver.session = MagicMock(return_value=session)
    driver.close = AsyncMock()
    return driver


@pytest.fixture
def mock_llm_client() -> MagicMock:
    """默认情况下允许使用真实 LLM；如需 mock 可在用例内显式 patch。"""
    # 当 .env.test 配置了真实 API Key 时，返回 None 表示不解 mock，让代码调用真实模型。
    if os.environ.get("LLM_AGENT_API_KEY"):
        yield None
        return
    # 未配置 Key 时保持原有 mock 行为
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


@pytest.fixture
def sample_doc_dir() -> Path:
    """返回 sample_documents 目录路径。"""
    return Path(__file__).resolve().parents[3] / "sample_documents"
