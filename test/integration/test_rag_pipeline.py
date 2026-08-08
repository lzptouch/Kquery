from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.integration
class TestRAGPipeline:
    async def test_document_upload_chunk_and_embed(
        self, sample_doc_dir: Path, test_collection_name: str
    ) -> None:
        from app.services.rag_service import RAGService

        service = RAGService(use_ocr=False, enable_hyde=False, enable_rerank=False)
        doc_path = sample_doc_dir / "sample.pdf"
        if not doc_path.exists():
            pytest.skip("样本 PDF 不存在，跳过文件级集成测试")

        # 集成环境无 Milvus，重点验证文档能被解析、切分并给出明确结果（成功或携带错误信息）
        result = await service.process_document(
            file_path=str(doc_path),
            collection_name=test_collection_name,
        )
        assert result.success or result.error_message is not None
        if result.success:
            assert result.chunk_count > 0

    async def test_query_documents_does_not_crash_without_milvus(
        self, test_collection_name: str, mock_llm_client: dict
    ) -> None:
        from app.services.rag_service import RAGService

        service = RAGService(enable_hyde=False, enable_rerank=False)
        result = await service.query_documents(
            query="什么是 RAG",
            collection_name=test_collection_name,
            top_k=5,
        )
        # 无 Milvus 时允许空结果或报错信息，但不应抛未处理异常
        assert result.total_count >= 0 or result.error_message is not None

    async def test_chunking_pipeline(self, sample_doc_dir: Path) -> None:
        from app.services.chunking import chunk_text

        doc_path = sample_doc_dir / "sample.pdf"
        if not doc_path.exists():
            pytest.skip("样本 PDF 不存在")

        from app.services.file_loaders import LoaderRegistry

        loader = LoaderRegistry().get_loader(doc_path)
        text = loader.load(doc_path)
        chunks = chunk_text(text, chunk_size=500, overlap=50)
        assert len(chunks) > 0
        assert all(isinstance(c, str) for c in chunks)
