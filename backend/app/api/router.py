"""
KQuery API 路由

定义所有RESTful API端点，提供知识库查询服务和智能问答功能
"""

import os
import time
import uuid
from typing import List, Optional
from fastapi import APIRouter, HTTPException, UploadFile, File, Query, Depends, Request
from pydantic import BaseModel, Field

from app.services.rag_service import RAGService
from app.services.storage_service import get_storage_service
from app.models.document import DocumentProcessResult
from app.models.query import QueryResult
from app.api.agent_router import router as agent_router
from app.core.logger import get_logger
from app.core.settings import get_settings
from app.core.security import validate_upload_file, UPLOAD_ALLOWED_EXTENSIONS, UPLOAD_MAX_FILE_SIZE
from app.core.rate_limiter import get_rate_limit_dependency
from app.core.audit_logger import audit_logger, AuditEventType
from app.core.audit_storage import audit_log_storage



logger = get_logger(__name__)
settings = get_settings()

# 主路由 - 集成所有功能
router = APIRouter()

# 包含原有的RAG路由
rag_router = APIRouter(prefix="/kquery", tags=["知识库查询"])

# 包含Agent路由（agent_router 已在其他模块中完成端点注册）
router.include_router(agent_router)


def get_rag_service() -> RAGService:
    """依赖注入：获取 RAGService 实例"""
    return RAGService()





@rag_router.post(
    "/documents/process",
    response_model=DocumentProcessResult,
    dependencies=[Depends(get_rate_limit_dependency(
        requests=30, window_seconds=3600, key_prefix="upload"
    ))],
)
async def process_document(
    request: Request,
    file: UploadFile = File(..., description="上传的文档文件"),
    collection_name: str = Query(..., description="向量集合名称"),
    chunk_size: int = Query(500, description="文本块大小"),
    chunk_overlap: int = Query(50, description="块重叠大小"),
    metadata: Optional[str] = Query(None, description="额外元数据（JSON格式）"),
    service: RAGService = Depends(get_rag_service)
):
    """处理单个文档

    将文档读入、切分、向量化并存储到向量数据库
    """
    start_time = time.perf_counter()
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    try:
        # 集中式文件上传安全验证（v2.1 新增）
        content, safe_filename = await validate_upload_file(
            file,
            allowed_extensions=UPLOAD_ALLOWED_EXTENSIONS,
            max_file_size=settings.security.max_file_size_mb * 1024 * 1024,
        )

        # 使用存储服务保存文件
        storage = get_storage_service()
        await storage.save(content, safe_filename)

        # 获取文件的本地路径（供 RAGService 读取）
        file_path = await storage.get_local_path(safe_filename)
        logger.info(f"文件已保存到存储后端: {safe_filename}")

        # 处理metadata参数
        metadata_dict = None
        if metadata:
            try:
                import json
                metadata_dict = json.loads(metadata)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="metadata参数必须是有效的JSON格式")

        # 处理文档
        result = await service.process_document(
            file_path=file_path,
            collection_name=collection_name,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            metadata=metadata_dict
        )

        # 记录文档处理日志
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_event(AuditEvent(
            event_type=AuditEventType.DOCUMENT_UPLOAD,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            resource="/api/v1/kquery/documents/process",
            action="process_document",
            details={
                "filename": safe_filename,
                "collection_name": collection_name,
                "chunk_count": result.chunk_count,
                "embedding_count": result.embedding_count,
                "duration_ms": duration_ms,
                "method": request.method,
            },
            status="success",
            request_id=request_id,
            severity="info",
            duration_ms=duration_ms,
        ))
        await audit_log_storage.flush()

        # 如果配置了自动清理，则删除本地临时文件
        if settings.storage.auto_cleanup:
            try:
                os.unlink(file_path)
                logger.info(f"临时文件已清理: {file_path}")
            except Exception as cleanup_error:
                logger.warning(f"临时文件清理失败: {cleanup_error}")

        return result

    except HTTPException:
        raise
    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_event(AuditEvent(
            event_type=AuditEventType.DOCUMENT_UPLOAD,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            resource="/api/v1/kquery/documents/process",
            action="process_document",
            details={
                "collection_name": collection_name,
                "duration_ms": duration_ms,
                "error": str(e),
                "method": request.method,
            },
            status="failure",
            request_id=request_id,
            severity="error",
            duration_ms=duration_ms,
        ))
        await audit_log_storage.flush()
        logger.error(f"文档处理失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档处理失败: {str(e)}")



@rag_router.post(
    "/documents/process-and-build",
    dependencies=[Depends(get_rate_limit_dependency(
        requests=20, window_seconds=3600, key_prefix="upload_build"
    ))],
)
async def process_document_and_build_kg(
    file: UploadFile = File(..., description="上传的文档文件"),
    collection_name: str = Query(..., description="向量集合名称"),
    chunk_size: int = Query(500, description="文本块大小"),
    chunk_overlap: int = Query(50, description="块重叠大小"),
    metadata: Optional[str] = Query(None, description="额外元数据（JSON格式）"),
    enable_kg: bool = Query(True, description="是否启用知识图谱构建"),
    service: RAGService = Depends(get_rag_service)
):
    """处理文档并自动构建知识图谱

    1. 上传文档并进行RAG处理（向量化存储）
    2. 自动从文档文本中抽取实体和关系
    3. 将知识图谱存入Neo4j
    """
    try:
        content, safe_filename = await validate_upload_file(
            file,
            allowed_extensions=UPLOAD_ALLOWED_EXTENSIONS,
            max_file_size=settings.security.max_file_size_mb * 1024 * 1024,
        )

        # 使用存储服务保存文件
        storage = get_storage_service()
        await storage.save(content, safe_filename)

        # 获取文件的本地路径（供 RAGService 读取）
        file_path = await storage.get_local_path(safe_filename)
        logger.info(f"文件已保存到存储后端: {safe_filename}")
        
        metadata_dict = None
        if metadata:
            import json
            metadata_dict = json.loads(metadata)
        
        # 1. RAG处理
        rag_result = await service.process_document(
            file_path=file_path,
            collection_name=collection_name,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            metadata=metadata_dict
        )
        
        kg_result = None
        if enable_kg and rag_result.success:
            try:
                # 2. 使用新的 build_graph_from_file 自动按文档类型路由
                from app.services.graph.graph_builder import GraphBuilderService
                
                builder = GraphBuilderService()
                
                build_result = await builder.build_graph_from_file(
                    file_path=file_path,
                    doc_id=safe_filename,
                    kb_id=None,
                    use_agent=False,
                    detect_communities=True,
                )
                
                kg_result = {
                    "entities_created": build_result.entities_created,
                    "entities_updated": build_result.entities_updated,
                    "relations_created": build_result.relations_created,
                    "communities_detected": build_result.communities_detected,
                    "entity_count_after": build_result.entity_count_after,
                    "relation_count_after": build_result.relation_count_after,
                    "doc_id": build_result.doc_id,
                    "classification": build_result.errors[0] if build_result.errors else None,
                }
                logger.info(f"知识图谱构建完成: {kg_result}")
            except Exception as kg_error:
                logger.warning(f"知识图谱构建失败（不影响RAG）: {kg_error}", exc_info=True)
                kg_result = {"error": str(kg_error)}
        
        if settings.storage.auto_cleanup:
            try:
                os.unlink(file_path)
            except Exception as cleanup_error:
                logger.warning(f"临时文件清理失败: {cleanup_error}")
        
        return {
            "success": rag_result.success,
            "rag": {
                "file_path": rag_result.file_path,
                "chunk_count": rag_result.chunk_count,
                "embedding_count": rag_result.embedding_count,
                "collection_name": rag_result.collection_name,
            },
            "kg_build": kg_result,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文档处理+KG构建失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档处理+KG构建失败: {str(e)}")


@rag_router.post(
    "/documents/process-batch",
    dependencies=[Depends(get_rate_limit_dependency(
        requests=30, window_seconds=3600, key_prefix="upload_batch"
    ))],
)
async def process_documents_batch(
    request: Request,
    files: List[UploadFile] = File(..., description="批量上传的文档文件"),
    collection_name: str = Query(..., description="向量集合名称"),
    chunk_size: int = Query(500, description="文本块大小"),
    chunk_overlap: int = Query(50, description="块重叠大小"),
    metadata: Optional[str] = Query(None, description="额外元数据（JSON格式）"),
    max_concurrency: int = Query(3, description="最大并发数"),
    service: RAGService = Depends(get_rag_service)
):
    """批量处理文档

    并发处理多个文档，提高处理效率
    """
    start_time = time.perf_counter()
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    try:
        storage = get_storage_service()

        # 保存所有上传的文件到存储后端
        file_paths = []
        for file in files:
            # 集中式文件上传安全验证（v2.1 新增）
            content, safe_filename = await validate_upload_file(
                file,
                allowed_extensions=UPLOAD_ALLOWED_EXTENSIONS,
                max_file_size=settings.security.max_file_size_mb * 1024 * 1024,
            )

            # 使用存储服务保存文件
            await storage.save(content, safe_filename)

            # 获取本地临时路径
            local_path = await storage.get_local_path(safe_filename)
            file_paths.append(local_path)
            logger.info(f"文件已保存到存储后端: {safe_filename}")
        
        # 处理metadata参数
        metadata_dict = None
        if metadata:
            try:
                import json
                metadata_dict = json.loads(metadata)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="metadata参数必须是有效的JSON格式")
        
        # 批量处理文档
        results = await service.process_documents_batch(
            file_paths=file_paths,
            collection_name=collection_name,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            metadata=metadata_dict,
            max_concurrency=max_concurrency
        )
        
        # 记录批量文档处理日志
        duration_ms = (time.perf_counter() - start_time) * 1000
        success_count = sum(1 for r in results if r.success)
        total_chunks = sum(r.chunk_count for r in results)
        total_embeddings = sum(r.embedding_count for r in results)
        await audit_logger.log_event(AuditEvent(
            event_type=AuditEventType.DOCUMENT_UPLOAD,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            resource="/api/v1/kquery/documents/process-batch",
            action="process_documents_batch",
            details={
                "file_count": len(files),
                "success_count": success_count,
                "failed_count": len(files) - success_count,
                "collection_name": collection_name,
                "total_chunks": total_chunks,
                "total_embeddings": total_embeddings,
                "duration_ms": duration_ms,
                "method": request.method,
            },
            status="success",
            request_id=request_id,
            severity="info",
            duration_ms=duration_ms,
        ))
        await audit_log_storage.flush()
        
        # 如果配置了自动清理，则删除本地临时文件
        if settings.storage.auto_cleanup:
            for file_path in file_paths:
                try:
                    os.unlink(file_path)
                    logger.info(f"临时文件已清理: {file_path}")
                except Exception as cleanup_error:
                    logger.warning(f"临时文件清理失败: {cleanup_error}")
        
        return {
            "total_files": len(files),
            "success_count": success_count,
            "failed_count": len(files) - success_count,
            "results": [
                {
                    "file_path": r.file_path,
                    "success": r.success,
                    "chunk_count": r.chunk_count,
                    "embedding_count": r.embedding_count,
                    "error_message": r.error_message
                }
                for r in results
            ]
        }
        
    except HTTPException:
        raise
    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_event(AuditEvent(
            event_type=AuditEventType.DOCUMENT_UPLOAD,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            resource="/api/v1/kquery/documents/process-batch",
            action="process_documents_batch",
            details={
                "file_count": len(files),
                "collection_name": collection_name,
                "duration_ms": duration_ms,
                "error": str(e),
                "method": request.method,
            },
            status="failure",
            request_id=request_id,
            severity="error",
            duration_ms=duration_ms,
        ))
        await audit_log_storage.flush()
        logger.error(f"批量文档处理失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"批量文档处理失败: {str(e)}")


class QueryDocumentsRequest(BaseModel):
    """文档查询请求体"""
    query: str = Field(..., description="查询文本")
    collection_name: str = Field(..., description="向量集合名称")
    top_k: int = Field(5, ge=1, le=100, description="返回结果数量")
    enable_hyde: bool = Field(False, description="是否启用HyDE")


@rag_router.post("/documents/query")
async def query_documents(
    request: Request,
    request_body: QueryDocumentsRequest,
    service: RAGService = Depends(get_rag_service)
):
    """查询文档

    基于向量相似度的语义检索
    """
    start_time = time.perf_counter()
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    try:
        if not request_body.query or not request_body.query.strip():
            raise HTTPException(status_code=400, detail="查询文本不能为空")

        if not request_body.collection_name or not request_body.collection_name.strip():
            raise HTTPException(status_code=400, detail="集合名称不能为空")

        if request_body.top_k < 1 or request_body.top_k > 100:
            raise HTTPException(status_code=400, detail="top_k 必须在 1 到 100 之间")

        result = await service.query_documents(
            query=request_body.query,
            collection_name=request_body.collection_name,
            top_k=request_body.top_k,
            enable_hyde=request_body.enable_hyde
        )

        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/kquery/documents/query",
            query_text=request_body.query,
            collection_name=request_body.collection_name,
            response_count=len(result.results),
            duration_ms=duration_ms,
            status="success",
            request_id=request_id,
        )

        return {
            "query": result.query,
            "results": [
                {
                    "id": r.id,
                    "text": r.content,
                    "score": r.score,
                    "metadata": r.metadata,
                }
                for r in result.results
            ],
            "total_count": result.total_count,
            "collection_name": result.collection_name,
        }

    except HTTPException:
        raise
    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/kquery/documents/query",
            query_text=request_body.query if request_body else None,
            collection_name=request_body.collection_name if request_body else None,
            duration_ms=duration_ms,
            status="failure",
            request_id=request_id,
        )
        logger.error(f"文档查询失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档查询失败: {str(e)}")



@rag_router.post("/documents/add-text")
async def add_text(
    text: str = Query(..., description="要添加的文本"),
    collection_name: str = Query(..., description="向量集合名称"),
    metadata: Optional[str] = Query(None, description="元数据（JSON格式）"),
    service: RAGService = Depends(get_rag_service)
):
    """直接添加文本到向量数据库
    
    跳过文件处理步骤，直接向量化并存储文本
    """
    try:
        # 验证输入
        if not text or not text.strip():
            raise HTTPException(status_code=400, detail="文本内容不能为空")
        
        if not collection_name or not collection_name.strip():
            raise HTTPException(status_code=400, detail="集合名称不能为空")
        
        # 处理metadata参数
        metadata_dict = None
        if metadata:
            try:
                import json
                metadata_dict = json.loads(metadata)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="metadata参数必须是有效的JSON格式")
        
        success = await service.add_text(
            text=text,
            collection_name=collection_name,
            metadata=metadata_dict
        )
        
        return {"success": success}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"文本添加失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"文本添加失败: {str(e)}")


@rag_router.get("/collections/{collection_name}/stats")
async def get_collection_stats(
    collection_name: str,
    service: RAGService = Depends(get_rag_service)
):
    """获取集合统计信息"""
    try:
        if not collection_name or not collection_name.strip():
            raise HTTPException(status_code=400, detail="集合名称不能为空")
        
        stats = await service.get_collection_stats(collection_name)
        return stats
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取集合统计失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取集合统计失败: {str(e)}")


@rag_router.delete("/collections/{collection_name}")
async def clear_collection(
    collection_name: str,
    service: RAGService = Depends(get_rag_service)
):
    """清空指定集合的所有数据
    
    警告：此操作不可逆
    """
    try:
        if not collection_name or not collection_name.strip():
            raise HTTPException(status_code=400, detail="集合名称不能为空")
        
        success = await service.clear_collection(collection_name)
        return {"success": success}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"清空集合失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"清空集合失败: {str(e)}")


@rag_router.get("/supported-file-types")
async def get_supported_file_types(
    service: RAGService = Depends(get_rag_service)
):
    """获取支持的文件类型列表"""
    try:
        file_types = service.get_supported_file_types()
        return {
            "supported_file_types": file_types,
            "allowed_extensions": list(UPLOAD_ALLOWED_EXTENSIONS),
            "max_file_size_mb": UPLOAD_MAX_FILE_SIZE // (1024 * 1024)
        }
        
    except Exception as e:
        logger.error(f"获取支持的文件类型失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取支持的文件类型失败: {str(e)}")


@rag_router.get("/collections")
async def list_collections(
    service: RAGService = Depends(get_rag_service)
):
    """列出所有向量集合"""
    try:
        collections = await service.list_collections()
        return {"collections": collections}
        
    except Exception as e:
        logger.error(f"列出集合失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"列出集合失败: {str(e)}")


@rag_router.get("/documents/{collection_name}")
async def list_documents(
    collection_name: str,
    limit: int = Query(100, description="返回数量限制"),
    offset: int = Query(0, description="偏移量"),
    service: RAGService = Depends(get_rag_service)
):
    """列出集合中的文档（基于元数据）"""
    try:
        result = await service.list_documents(
            collection_name=collection_name,
            limit=limit,
            offset=offset,
        )
        if result["total"] == 0 and not result.get("note"):
            raise HTTPException(status_code=404, detail=f"集合 '{collection_name}' 不存在")
        return result
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"列出文档失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"列出文档失败: {str(e)}")


@rag_router.get("/status")
async def get_service_status(
    service: RAGService = Depends(get_rag_service)
):
    """获取服务状态"""
    try:
        status = await service.get_service_status()
        return status
        
    except Exception as e:
        logger.error(f"获取服务状态失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取服务状态失败: {str(e)}")


# 在 rag_router 所有端点定义完成后，再挂载到主 router
router.include_router(rag_router)
