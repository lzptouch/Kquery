"""统一的RAG服务接口。

提供文档读入、切分、向量化、存储和查询的一站式服务。
整合所有RAG相关功能，提供简洁易用的API。

主要功能：
- 文档加载与处理：支持多种文件格式（PDF、Word、Excel、PowerPoint、HTML、Markdown、Text、XML、JSON、图片等）
- 文档切分：智能文本分块
- 向量化：生成embedding向量
- 向量存储：存储到Milvus向量数据库
- 语义检索：文档检索和查询
- 记忆管理：会话记忆和意图分析

支持的文件格式：
- 基础文本：.txt, .md, .markdown
- 办公文档：.pdf, .docx, .doc, .xlsx, .xls, .pptx, .ppt
- 网页格式：.html, .htm
- 数据格式：.xml, .xsl, .xsd, .dtd, .json, .jsonl
- 图片格式：.jpg, .jpeg, .png, .bmp, .tiff, .tif, .gif, .webp

使用示例：
    from app import RAGService
    
    # 初始化服务
    service = RAGService()
    
    # 处理文档（读入、切分、向量化、存储）
    result = await service.process_document(
        file_path="document.pdf",
        collection_name="my_collection"
    )
    
    # 查询文档
    results = await service.query_documents(
        query="什么是机器学习？",
        collection_name="my_collection",
        top_k=5
    )
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass, field

from app.core.logger import get_logger
from app.services.file_loaders import LoaderRegistry, DocumentChunk, ChunkType
from app.services.chunking import chunk_text
from app.services.embedding import OpenAIEmbeddingService
from app.services.retriever import RAGEngine, RetrievalResult
from app.services.file_loaders.pdf_loader import PDFLoader
from app.services.file_loaders.word_loader import WordLoader
from app.services.file_loaders.markdown_loader import MarkdownLoader
from app.repositories.operations import (
    insert_document_chunk,
    query_document_chunks,
    clear_document_chunks,
    get_collection_stats,
)

logger = get_logger(__name__)


@dataclass
class DocumentProcessResult:
    """文档处理结果。
    
    Attributes:
        success: 是否成功
        file_path: 文件路径
        chunk_count: 生成的块数量
        embedding_count: 生成的向量数量
        collection_name: 集合名称
        error_message: 错误消息（如果有）
    """
    success: bool
    file_path: str
    chunk_count: int
    embedding_count: int
    collection_name: str
    error_message: Optional[str] = None


@dataclass
class QueryResult:
    """查询结果。
    
    Attributes:
        query: 查询文本
        results: 检索结果列表
        total_count: 总结果数
        collection_name: 集合名称
        error_message: 错误消息（如果有）
    """
    query: str
    results: List[RetrievalResult]
    total_count: int
    collection_name: str
    error_message: Optional[str] = None


class RAGService:
    """统一的RAG服务接口。
    
    提供文档处理、向量化、存储和查询的一站式服务。
    """
    
    def __init__(
        self,
        use_ocr: bool = False,
        preserve_structure: bool = True,
        enable_hyde: bool = True,
        enable_rerank: bool = True,
        extract_images: bool = True,
        understand_images: bool = True,
        extract_tables: bool = True,
    ):
        """初始化RAG服务。
        
        Args:
            use_ocr: 是否启用OCR（仅对支持OCR的加载器有效）
            preserve_structure: 是否保留文档结构
            enable_hyde: 是否启用HyDE（假设性文档嵌入）
            enable_rerank: 是否启用重排序
            extract_images: 是否提取文档中的图像
            understand_images: 是否对提取的图像进行AI理解
            extract_tables: 是否提取和处理表格
        """
        self.use_ocr = use_ocr
        self.preserve_structure = preserve_structure
        self.enable_hyde = enable_hyde
        self.enable_rerank = enable_rerank
        self.extract_images = extract_images
        self.understand_images = understand_images
        self.extract_tables = extract_tables
        
        # 初始化服务组件
        self._embedding_service: Optional[OpenAIEmbeddingService] = None
        self._rag_engine: Optional[RAGEngine] = None
        
        logger.info("RAG服务初始化完成")
    
    def _get_embedding_service(self) -> OpenAIEmbeddingService:
        """获取嵌入服务实例。"""
        if self._embedding_service is None:
            self._embedding_service = OpenAIEmbeddingService()
            logger.info("嵌入服务初始化完成")
        return self._embedding_service
    
    def _get_rag_engine(self) -> RAGEngine:
        """获取RAG引擎实例。"""
        if self._rag_engine is None:
            self._rag_engine = RAGEngine(
                enable_hyde=self.enable_hyde,
                enable_rerank=self.enable_rerank
            )
            logger.info("RAG引擎初始化完成")
        return self._rag_engine
    
    async def process_document_with_chunks(
        self,
        file_path: Union[str, Path],
        collection_name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DocumentProcessResult:
        """处理文档：使用新的DocumentChunk结构读入、切分、向量化、存储到向量数据库。
        
        Args:
            file_path: 文件路径
            collection_name: 向量集合名称
            metadata: 额外的元数据
        
        Returns:
            DocumentProcessResult: 处理结果
        """
        file_path = Path(file_path)
        
        try:
            logger.info(f"开始处理文档: {file_path}")
            
            # 1. 读取文件
            if not file_path.exists():
                raise FileNotFoundError(f"文件不存在: {file_path}")
            
            logger.info(f"步骤1: 读取文件 {file_path.name}")
            
            # 根据文件类型选择合适的加载器
            file_extension = file_path.suffix.lower()
            loader = None
            
            if file_extension == '.pdf':
                loader = PDFLoader(
                    use_ocr=self.use_ocr,
                    preserve_structure=self.preserve_structure,
                    extract_images=self.extract_images,
                    understand_images=self.understand_images
                )
            elif file_extension in ['.docx', '.doc']:
                loader = WordLoader(
                    use_ocr=self.use_ocr,
                    preserve_structure=self.preserve_structure,
                    extract_images=self.extract_images,
                    understand_images=self.understand_images,
                    extract_tables=self.extract_tables
                )
            elif file_extension in ['.md', '.markdown']:
                loader = MarkdownLoader(
                    use_ocr=self.use_ocr,
                    preserve_structure=self.preserve_structure,
                    extract_images=self.extract_images,
                    understand_images=self.understand_images
                )
            else:
                # 使用通用加载器
                loader = LoaderRegistry.get_loader(
                    file_path,
                    use_ocr=self.use_ocr,
                    preserve_structure=self.preserve_structure
                )
            
            # 加载文档块
            if hasattr(loader, 'load_document_chunks'):
                chunks = loader.load_document_chunks(file_path)
            else:
                # 降级到旧的加载方式
                content = loader.load(file_path)
                if not content or not content.strip():
                    raise ValueError(f"文件内容为空: {file_path}")
                
                from app.services.chunking import chunk_text
                text_chunks = chunk_text(text=content, chunk_size=500, chunk_overlap=50)
                search_start = 0
                chunks = []
                for i, tc in enumerate(text_chunks):
                    offset = content.find(tc, search_start)
                    if offset == -1:
                        offset = search_start
                    search_start = offset + 1
                    chunks.append(
                        DocumentChunk(
                            content=tc,
                            metadata=loader.metadata.copy() if loader.metadata else {},
                            chunk_type=ChunkType.TEXT,
                            source_file=str(file_path),
                            char_offset=offset
                        )
                    )
            
            chunk_count = len(chunks)
            logger.info(f"文档加载完成，生成 {chunk_count} 个文档块")
            
            # 2. 向量化
            logger.info(f"步骤2: 生成嵌入向量")
            embedding_service = self._get_embedding_service()
            
            # 提取所有文档块的完整内容（包含图像/表格描述）
            texts = [chunk.get_full_content() for chunk in chunks]
            
            # 生成嵌入向量
            vectors = await embedding_service.embed_parallel(texts)
            
            embedding_count = len(vectors)
            logger.info(f"嵌入向量生成完成，共 {embedding_count} 个向量")
            
            # 3. 存储到向量数据库
            logger.info(f"步骤3: 存储到向量数据库集合 '{collection_name}'")
            
            # 构建文档块数据
            document_chunks = []
            for i, (chunk, vector) in enumerate(zip(chunks, vectors)):
                chunk_metadata = chunk.metadata.copy()
                chunk_metadata.update({
                    "file_path": str(file_path),
                    "file_name": file_path.name,
                    "chunk_index": i,
                    "chunk_type": chunk.chunk_type.value,
                    "chunk_id": chunk.chunk_id,
                    "page_number": chunk.page_number,
                    "char_offset": chunk.char_offset,
                })
                
                # 添加图像和表格信息到元数据
                if chunk.image_path:
                    chunk_metadata["image_path"] = chunk.image_path
                if chunk.image_description:
                    chunk_metadata["image_description"] = chunk.image_description
                if chunk.table_data:
                    chunk_metadata["table_data"] = chunk.table_data
                if chunk.table_description:
                    chunk_metadata["table_description"] = chunk.table_description
                
                # 添加用户自定义元数据
                if metadata:
                    chunk_metadata.update(metadata)
                
                # 插入到数据库
                success = await insert_document_chunk(
                    collection_name=collection_name,
                    text=chunk.get_full_content(),
                    vector=vector,
                    metadata=chunk_metadata,
                )
                
                if success:
                    document_chunks.append({
                        "text": chunk.get_full_content(),
                        "vector": vector,
                        "metadata": chunk_metadata,
                    })
            
            actual_count = len(document_chunks)
            logger.info(f"文档处理完成: {file_path}, 块数: {actual_count}, 向量数: {embedding_count}")
            
            return DocumentProcessResult(
                success=True,
                file_path=str(file_path),
                chunk_count=actual_count,
                embedding_count=embedding_count,
                collection_name=collection_name,
            )
        
        except Exception as e:
            logger.error(f"文档处理失败: {file_path}, 错误: {e}", exc_info=True)
            return DocumentProcessResult(
                success=False,
                file_path=str(file_path),
                chunk_count=0,
                embedding_count=0,
                collection_name=collection_name,
                error_message=str(e),
            )
    
    async def process_document(
        self,
        file_path: Union[str, Path],
        collection_name: str,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DocumentProcessResult:
        """处理文档：读入、切分、向量化、存储到向量数据库。
        
        保留旧接口，内部调用新的 process_document_with_chunks 方法。
        
        Args:
            file_path: 文件路径
            collection_name: 向量集合名称
            chunk_size: 块大小（字符数）（向后兼容，已忽略）
            chunk_overlap: 块重叠大小（字符数）（向后兼容，已忽略）
            metadata: 额外的元数据
        
        Returns:
            DocumentProcessResult: 处理结果
        """
        return await self.process_document_with_chunks(
            file_path=file_path,
            collection_name=collection_name,
            metadata=metadata
        )
    
    async def process_documents_batch(
        self,
        file_paths: List[Union[str, Path]],
        collection_name: str,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        metadata: Optional[Dict[str, Any]] = None,
        max_concurrency: int = 3,
    ) -> List[DocumentProcessResult]:
        """批量处理文档。
        
        Args:
            file_paths: 文件路径列表
            collection_name: 向量集合名称
            chunk_size: 块大小（字符数）
            chunk_overlap: 块重叠大小（字符数）
            metadata: 额外的元数据
            max_concurrency: 最大并发数
        
        Returns:
            List[DocumentProcessResult]: 处理结果列表
        
        Example:
            results = await service.process_documents_batch(
                file_paths=["doc1.pdf", "doc2.docx"],
                collection_name="my_collection"
            )
        """
        logger.info(f"开始批量处理 {len(file_paths)} 个文档，并发数: {max_concurrency}")
        
        # 使用信号量控制并发
        semaphore = asyncio.Semaphore(max_concurrency)
        
        async def process_with_semaphore(file_path: Union[str, Path]) -> DocumentProcessResult:
            """带信号量控制的文档处理。"""
            async with semaphore:
                return await self.process_document(
                    file_path=file_path,
                    collection_name=collection_name,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    metadata=metadata,
                )
        
        # 并发处理所有文档
        tasks = [process_with_semaphore(fp) for fp in file_paths]
        results = await asyncio.gather(*tasks)
        
        success_count = sum(1 for r in results if r.success)
        logger.info(f"批量处理完成: 成功 {success_count}/{len(file_paths)}")
        
        return results
    
    async def query_documents(
        self,
        query: str,
        collection_name: str,
        top_k: int = 5,
        enable_hyde: bool = True,
    ) -> QueryResult:
        """查询文档。
        
        Args:
            query: 查询文本
            collection_name: 向量集合名称
            top_k: 返回结果数量
            enable_hyde: 是否启用HyDE（假设性文档嵌入）
        
        Returns:
            QueryResult: 查询结果
        
        Example:
            result = await service.query_documents(
                query="什么是机器学习？",
                collection_name="my_collection",
                top_k=5
            )
        """
        try:
            logger.info(f"查询文档: '{query}', 集合: '{collection_name}', top_k: {top_k}")
            
            # 使用RAG引擎进行检索
            rag_engine = self._get_rag_engine()
            
            results = await rag_engine.search(
                query=query,
                collection_name=collection_name,
                top_k=top_k,
                enable_hyde=enable_hyde,
            )
            
            logger.info(f"查询完成，返回 {len(results)} 个结果")
            
            return QueryResult(
                query=query,
                results=results,
                total_count=len(results),
                collection_name=collection_name,
            )
        
        except Exception as e:
            logger.error(f"查询失败: {query}, 错误: {e}", exc_info=True)
            return QueryResult(
                query=query,
                results=[],
                total_count=0,
                collection_name=collection_name,
                error_message=str(e),
            )
    
    async def clear_collection(self, collection_name: str) -> bool:
        """清空指定集合的所有数据。
        
        Args:
            collection_name: 集合名称
        
        Returns:
            bool: 是否成功
        
        Example:
            success = await service.clear_collection("my_collection")
        """
        try:
            logger.info(f"清空集合: '{collection_name}'")
            success = await clear_document_chunks(collection_name)
            if success:
                logger.info(f"集合清空完成: '{collection_name}'")
            return success
        except Exception as e:
            logger.error(f"清空集合失败: '{collection_name}', 错误: {e}", exc_info=True)
            return False
    
    async def get_collection_stats(self, collection_name: str) -> Dict[str, Any]:
        """获取集合统计信息。
        
        Args:
            collection_name: 集合名称
        
        Returns:
            Dict[str, Any]: 统计信息
        
        Example:
            stats = await service.get_collection_stats("my_collection")
            print(f"文档数量: {stats.get('count', 0)}")
        """
        try:
            stats = await get_collection_stats(collection_name)
            logger.info(f"获取集合统计成功: '{collection_name}', 文档数: {stats.get('count', 0)}")
            return stats
        except Exception as e:
            logger.error(f"获取集合统计失败: '{collection_name}', 错误: {e}", exc_info=True)
            return {
                "collection_name": collection_name,
                "count": 0,
                "error": str(e),
            }
    
    async def add_text(
        self,
        text: str,
        collection_name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """直接添加文本到向量数据库。
        
        Args:
            text: 文本内容
            collection_name: 集合名称
            metadata: 元数据
        
        Returns:
            bool: 是否成功
        
        Example:
            success = await service.add_text(
                text="这是一段文本",
                collection_name="my_collection",
                metadata={"source": "manual"}
            )
        """
        try:
            if not text or not text.strip():
                raise ValueError("文本内容不能为空")
            
            logger.info(f"添加文本到集合 '{collection_name}', 文本长度: {len(text)}")
            
            # 使用RAG引擎插入文本
            rag_engine = self._get_rag_engine()
            success = await rag_engine.insert_document_chunk(
                text=text,
                collection_name=collection_name,
                metadata=metadata,
            )
            
            if success:
                logger.info("文本添加成功")
            else:
                logger.warning("文本添加失败")
            
            return success
        
        except Exception as e:
            logger.error(f"添加文本失败: {e}", exc_info=True)
            return False
    
    async def list_collections(self) -> List[str]:
        """列出所有向量集合。

        Returns:
            List[str]: 集合名称列表
        """
        from app.repositories.database.milvus_connector import get_milvus_connector
        connector = get_milvus_connector()
        return connector.list_collections()

    async def list_documents(
        self,
        collection_name: str,
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        """列出集合中的文档。

        Args:
            collection_name: 集合名称
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            Dict[str, Any]: 文档列表
        """
        from pymilvus import Collection
        from app.repositories.database.milvus_connector import get_milvus_connector, get_milvus_connection

        connector = get_milvus_connector()
        connection_name = get_milvus_connection()

        if not connection_name or not connector.has_collection(collection_name):
            return {"documents": [], "total": 0, "collection_name": collection_name}

        collection = Collection(collection_name, using=connection_name)
        collection.load()

        try:
            results = collection.query(
                expr="id != ''",
                output_fields=["id", "text", "metadata"],
                limit=limit,
                offset=offset
            )

            docs_map = {}
            for r in results:
                metadata = r.get("metadata", {}) or {}
                filename = metadata.get("filename") or metadata.get("file_name") or metadata.get("source") or "未知文件"

                if filename not in docs_map:
                    docs_map[filename] = {
                        "id": filename,
                        "filename": filename,
                        "file_path": metadata.get("file_path", ""),
                        "chunk_count": 0,
                        "total_chars": 0,
                        "uploaded_at": metadata.get("uploaded_at", ""),
                    }

                docs_map[filename]["chunk_count"] += 1
                docs_map[filename]["total_chars"] += len(r.get("text", ""))

            documents = list(docs_map.values())
            return {
                "documents": documents,
                "total": len(documents),
                "collection_name": collection_name,
            }
        except Exception as query_error:
            logger.warning(f"查询文档元数据失败: {query_error}")
            return {
                "documents": [],
                "total": 0,
                "collection_name": collection_name,
                "note": "无法获取文档详情",
            }

    async def get_service_status(self) -> Dict[str, Any]:
        """获取服务状态。

        Returns:
            Dict[str, Any]: 服务状态信息
        """
        from app.repositories.database.milvus_connector import test_milvus_connection
        milvus_connected = test_milvus_connection()
        return {
            "service": "KQuery API",
            "status": "running",
            "milvus_connected": milvus_connected,
            "version": "1.0.0",
        }

    def get_supported_file_types(self) -> List[str]:
        """获取支持的文件类型列表。
        
        Returns:
            List[str]: 文件扩展名列表
        
        Example:
            types = service.get_supported_file_types()
            print(f"支持的文件类型: {', '.join(types)}")
        """
        return LoaderRegistry.get_supported_extensions()
    
    def is_file_supported(self, file_path: Union[str, Path]) -> bool:
        """检查文件类型是否支持。
        
        Args:
            file_path: 文件路径
        
        Returns:
            bool: 是否支持
        
        Example:
            if service.is_file_supported("document.pdf"):
                print("支持此文件类型")
        """
        return LoaderRegistry.is_supported(Path(file_path))