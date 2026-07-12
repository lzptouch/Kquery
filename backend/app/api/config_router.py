"""配置管理路由

提供配置热更新、查看等管理接口。

v2.1 新增
"""

from typing import Any, Dict
from fastapi import APIRouter, HTTPException, status

from app.core.settings import get_settings, reload_settings, Settings
from app.core.logger import get_logger


logger = get_logger(__name__)
router = APIRouter(prefix="/config", tags=["配置管理"])


@router.post("/reload", summary="重新加载配置")
async def reload_config(
) -> Dict[str, Any]:
    """重新加载配置（不重启服务）。
    
    从环境变量和 .env 文件重新读取配置，
    清除配置缓存并创建新的配置实例。
    
    Returns:
        包含新配置摘要的字典
        
    Raises:
        HTTPException: 配置验证失败时返回 400
    """
    try:
        old_settings = get_settings()
        old_env = old_settings.app.environment
        
        new_settings = reload_settings()
        new_env = new_settings.app.environment
        
        logger.info(
            f"配置已热重载 - 操作者: anonymous, "
            f"环境: {old_env} -> {new_env}"
        )
        
        return {
            "message": "配置已更新",
            "environment": new_env,
            "debug": new_settings.app.debug,
            "app_name": new_settings.app.name,
            "changed": old_env != new_env,
        }
    except Exception as e:
        logger.error(f"配置热重载失败: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"配置验证失败: {str(e)}"
        )


@router.get("/current", summary="查看当前配置")
async def get_current_config(
) -> Dict[str, Any]:
    """查看当前生效的配置（敏感信息已脱敏）。
    
    Returns:
        脱敏后的配置字典
    """
    settings = get_settings()
    
    def mask_secret(value: Any) -> Any:
        """脱敏敏感信息。"""
        if isinstance(value, str) and value:
            if len(value) > 8:
                return value[:4] + "****" + value[-4:]
            return "****"
        return value
    
    return {
        "app": {
            "name": settings.app.name,
            "environment": settings.app.environment,
            "debug": settings.app.debug,
            "api_host": settings.app.api_host,
            "api_port": settings.app.api_port,
            "enable_metrics": settings.app.enable_metrics,
        },
        "database": {
            "milvus_host": settings.database.milvus_host,
            "milvus_port": settings.database.milvus_port,
            "neo4j_uri": settings.database.neo4j_uri,
            "neo4j_database": settings.database.neo4j_database,
            "redis_url": mask_secret(settings.database.redis_url),
            "mongodb_url": mask_secret(settings.database.mongodb_url) if settings.database.mongodb_url else None,
        },
        "llm": {
            "embedding_model_name": settings.llm.embedding_model_name,
            "embedding_api_base": settings.llm.embedding_api_base,
            "embedding_timeout": settings.llm.embedding_timeout,
            "rerank_model_name": settings.llm.rerank_model_name,
            "rerank_api_base": settings.llm.rerank_api_base,
            "agent_model_name": settings.llm.agent_model_name,
        },
        "rag": {
            "initial_top_k": settings.rag.initial_top_k,
            "top_k": settings.rag.top_k,
            "use_hyde": settings.rag.use_hyde,
            "use_rrf": settings.rag.use_rrf,
            "use_reranker": settings.rag.use_reranker,
            "chunk_size": settings.rag.chunk_size,
            "chunk_overlap": settings.rag.chunk_overlap,
        },
        "security": {
            "rate_limit_requests": settings.security.rate_limit_requests,
            "rate_limit_window_seconds": settings.security.rate_limit_window_seconds,
            "max_file_size_mb": settings.security.max_file_size_mb,
            "api_key_enabled": settings.security.api_key_enabled,
        },
        "performance": {
            "batch_max_concurrency": settings.performance.batch_max_concurrency,
            "embedding_max_concurrency": settings.performance.embedding_max_concurrency,
            "query_timeout": settings.performance.query_timeout,
            "process_timeout": settings.performance.process_timeout,
        },
    }
