from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.rag_service import RAGService, DocumentProcessResult


@pytest.mark.unit
class TestRAGServiceLifecycle:
    async def test_init_defaults(self) -> None:
        service = RAGService()
        assert service.enable_hyde is True
        assert service.enable_rerank is True

    async def test_process_unsupported_file(self) -> None:
        service = RAGService()
        result = await service.process_document(
            file_path="/tmp/not_supported.xyz",
            collection_name="test_collection",
        )
        assert result.success is False
        assert result.error_message is not None

    async def test_query_documents_returns_result(self, mock_llm_client: dict) -> None:
        service = RAGService(enable_hyde=False, enable_rerank=False)
        # 模拟底层 RAGEngine 返回结果
        fake_result = type("QR", (), {
            "query": "什么是机器学习",
            "results": [],
            "total_count": 0,
            "collection_name": "test_collection",
            "error_message": None,
        })()
        fake_engine = MagicMock()
        fake_engine.query = AsyncMock(return_value=fake_result)
        with patch.object(service, "_get_rag_engine", return_value=fake_engine):
            result = await service.query_documents(
                query="什么是机器学习",
                collection_name="test_collection",
                top_k=5,
            )
            assert result.query == "什么是机器学习"

    async def test_process_document_mocks_external_calls(self, tmp_path: Path) -> None:
        service = RAGService(use_ocr=False, enable_hyde=False, enable_rerank=False)
        doc = tmp_path / "test.txt"
        doc.write_text("这是测试文档内容。" * 50)
        fake_engine = MagicMock()
        fake_engine.add_documents = AsyncMock(return_value=1)
        fake_emb = MagicMock()
        fake_emb.embed_texts = AsyncMock(return_value=[[0.1] * 768])
        with patch.object(service, "_get_rag_engine", return_value=fake_engine), \
             patch.object(service, "_get_embedding_service", return_value=fake_emb):
            result = await service.process_document(
                file_path=str(doc),
                collection_name="test_collection",
            )
            assert isinstance(result, DocumentProcessResult)
            assert result.collection_name == "test_collection"
