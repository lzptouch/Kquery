"""
数据库操作模块
提供简化的文档CRUD操作
"""

from typing import Any, Dict, List, Optional

from pymilvus import Collection, utility

from app.core.settings import get_settings
from app.repositories.database.milvus_connector import get_milvus_connection, get_milvus_connector
from app.core.logger import get_logger

settings = get_settings()

logger = get_logger(__name__)


def ensure_collection_exists(collection_name: str) -> Optional[Collection]:
    """确保集合存在，如果不存在则创建"""
    try:
        connector = get_milvus_connector()
        connection_name = get_milvus_connection()

        if not connection_name:
            logger.error("Milvus连接未初始化")
            return None

        # 检查集合是否存在
        if connector.has_collection(collection_name):
            return Collection(collection_name, using=connection_name)

        # 创建新的集合
        from pymilvus import CollectionSchema, DataType, FieldSchema

        # 定义字段
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=128, is_primary=True),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
            FieldSchema(
                name="embedding", dtype=DataType.FLOAT_VECTOR, dim=settings.llm.embedding_dimension
            ),
            FieldSchema(name="metadata", dtype=DataType.JSON),
        ]

        schema = CollectionSchema(fields=fields, description=f"文档集合: {collection_name}")

        collection = Collection(name=collection_name, schema=schema, using=connection_name)

        # 创建索引
        index_params = {
            "metric_type": "COSINE",
            "index_type": "HNSW",
            "params": {"M": 16, "efConstruction": 200},
        }
        collection.create_index(field_name="embedding", index_params=index_params)

        logger.info(f"集合 {collection_name} 创建成功")
        return collection

    except Exception as e:
        logger.error(f"确保集合存在失败: {collection_name}, 错误: {e}")
        return None


async def insert_document_chunk(
    collection_name: str,
    text: str,
    vector: List[float],
    metadata: Dict[str, Any] = None,
    chunk_id: Optional[str] = None,
) -> bool:
    """插入文档块"""
    try:
        collection = ensure_collection_exists(collection_name)
        if not collection:
            return False

        # 准备数据
        import uuid

        data = [
            {
                "id": chunk_id or str(uuid.uuid4()),
                "text": text,
                "embedding": vector,
                "metadata": metadata or {},
            }
        ]

        # 插入数据
        collection.insert(data)
        collection.flush()

        logger.debug(f"文档块插入成功: 集合={collection_name}, 文本长度={len(text)}")
        return True

    except Exception as e:
        logger.error(f"插入文档块失败: {e}")
        return False


async def query_document_chunks(
    query_text: str,
    collection_name: str,
    top_k: int = 5,
    query_vector: Optional[List[float]] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """查询文档块"""
    try:
        connector = get_milvus_connector()
        connection_name = get_milvus_connection()

        if not connection_name or not connector.has_collection(collection_name):
            logger.warning(f"集合不存在: {collection_name}")
            return []

        collection = Collection(collection_name, using=connection_name)

        # 加载集合
        collection.load()

        # 如果没有传入向量，则生成查询向量
        vector = query_vector
        if vector is None:
            from app.services.embedding import OpenAIEmbeddingService
            embedding_service = OpenAIEmbeddingService()
            vectors = await embedding_service.embed([query_text])
            vector = vectors[0]

        # 构建搜索参数
        search_params = {"metric_type": "COSINE", "params": {"ef": 50}}

        # 执行搜索
        results = collection.search(
            data=[vector],
            anns_field="embedding",
            param=search_params,
            limit=top_k,
            output_fields=["text", "metadata"],
        )

        # 格式化结果
        formatted_results = []
        for hits in results:
            for hit in hits:
                formatted_results.append(
                    {
                        "text": hit.entity.get("text", ""),
                        "score": hit.score,
                        "metadata": hit.entity.get("metadata", {}),
                        "id": hit.id,
                    }
                )

        return formatted_results

    except Exception as e:
        logger.error(f"查询文档块失败: {e}")
        return []


async def delete_document_chunk(collection_name: str, chunk_id: str) -> bool:
    """删除文档块"""
    try:
        connector = get_milvus_connector()
        connection_name = get_milvus_connection()

        if not connection_name or not connector.has_collection(collection_name):
            return False

        collection = Collection(collection_name, using=connection_name)

        # 删除数据
        collection.delete(f"id in ['{chunk_id}']")
        collection.flush()

        logger.debug(f"文档块删除成功: {chunk_id}")
        return True

    except Exception as e:
        logger.error(f"删除文档块失败: {e}")
        return False


async def clear_document_chunks(collection_name: str) -> bool:
    """清空集合中的所有文档块"""
    try:
        connector = get_milvus_connector()
        connection_name = get_milvus_connection()

        if not connection_name or not connector.has_collection(collection_name):
            return True  # 集合不存在，视为清空成功

        collection = Collection(collection_name, using=connection_name)

        # 获取所有数据的ID
        results = collection.query(expr="", output_fields=["id"])

        if results:
            ids = [result["id"] for result in results]
            # 批量删除
            collection.delete(f"id in {ids}")
            collection.flush()

        logger.info(f"集合清空成功: {collection_name}")
        return True

    except Exception as e:
        logger.error(f"清空集合失败: {e}")
        return False


async def get_collection_stats(collection_name: str) -> Dict[str, Any]:
    """获取集合统计信息"""
    try:
        connector = get_milvus_connector()
        connection_name = get_milvus_connection()

        if not connection_name or not connector.has_collection(collection_name):
            return {"collection_name": collection_name, "exists": False, "count": 0}

        collection = Collection(collection_name, using=connection_name)

        # 获取集合统计（PyMilvus 3.0 使用 num_entities）
        count = collection.num_entities
        
        # 获取集合的维度信息
        dimension = None
        try:
            schema = collection.schema
            for field in schema.fields:
                if field.name == "embedding":
                    dimension = field.params.get("dim") or field.params.get("dimension")
                    break
        except Exception:
            pass

        return {
            "collection_name": collection_name,
            "exists": True,
            "count": count,
            "dimension": dimension,
            "total_vector_count": count,
        }

    except Exception as e:
        logger.error(f"获取集合统计失败: {e}")
        return {"collection_name": collection_name, "exists": False, "count": 0, "error": str(e)}
