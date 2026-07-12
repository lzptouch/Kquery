"""
KQuery FastAPI 主应用

提供知识库查询服务的RESTful API接口
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

# 导入核心模块
from app.core.settings import get_settings
from app.core.middleware import RequestLoggingMiddleware
from app.core.logger import get_logger, LogstashHandler
from app.core.security import add_security_headers_middleware
from app.api.exception_handlers import register_exception_handlers
from app.core.rate_limiter import rate_limiter, rate_limit_middleware

# 性能优化模块（第三阶段新增）
from app.core.cache import ml_cache, redis_cache_service  # 多级缓存
from app.core.connection_pool import pool_manager          # 连接池管理
from app.core.cache_warmer import cache_warmer              # 缓存预热（新增）

# 导入路由模块
from app.api.router import router
from app.api.knowledge_graph import router as kg_router  # 新增：知识图谱接口（第四阶段）
from app.api.graphrag import router as graphrag_router    # 🆕 GraphRAG增强检索接口（第五阶段）
from app.api.config_router import router as config_router  # v2.1 新增：配置管理接口
from app.api.health import router as health_router
from app.api.chart_data import router as chart_router      # v2.0 新增：图表数据接口
from app.api.multimodal import router as multimodal_router  # v2.0 新增：多模态内容接口

logger = get_logger(__name__)
_elk_handler: LogstashHandler | None = None
settings = get_settings()

# 创建FastAPI应用
app = FastAPI(
    title="KQuery Pro API",
    description="知识库查询服务 - 提供多格式文档处理、语义检索和知识图谱功能",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# 注册全局异常处理器
register_exception_handlers(app)

# 添加安全响应头（防XSS、点击劫持等）
add_security_headers_middleware(app)

# 添加速率限制中间件（需要Redis支持）
app.middleware("http")(rate_limit_middleware)

# 添加请求日志中间件（开发环境启用）
if settings.debug:
    app.add_middleware(RequestLoggingMiddleware)

# 配置CORS（安全头之后）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.debug else settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 添加监控
if settings.enable_metrics:
    Instrumentator().instrument(app).expose(app)

# 注册路由
app.include_router(health_router)  # 健康检查接口（无前缀）
app.include_router(kg_router, prefix="/api/v1")  # 知识图谱接口（第四阶段新增）
app.include_router(graphrag_router, prefix="/api/v1")  # 🆕 GraphRAG增强检索接口（第五阶段新增）
app.include_router(config_router, prefix="/api/v1")  # v2.1 新增：配置管理接口
app.include_router(chart_router, prefix="/api/v1")  # v2.0 新增：图表数据接口
app.include_router(multimodal_router, prefix="/api/v1")  # v2.0 新增：多模态内容接口
app.include_router(router, prefix="/api/v1")  # 业务接口


@app.on_event("startup")
async def startup_event():
    """
    应用启动事件
    
    初始化Redis、数据库表、缓存、连接池等所有资源
    """
    import time
    start_time = time.time()
    
    logger.info("=" * 60)
    logger.info("KQuery Pro API 服务启动")
    logger.info(f"版本: 2.0.0")
    logger.info(f"环境: {settings.environment}")
    logger.info(f"调试模式: {settings.debug}")
    
    try:
        # 1. 初始化Redis连接（用于速率限制）
        await rate_limiter.init_redis()  
        logger.info("✓ Redis连接（限流）初始化成功")
        
        # 2. 初始化审计日志数据库表
        try:
            from app.core.audit_storage import audit_log_storage
            await audit_log_storage.initialize()
            logger.info("✓ 审计日志表初始化成功")
        except Exception as audit_err:
            logger.warning(f"⚠ 审计日志表初始化失败: {audit_err}")
        
        # 3. 初始化多级缓存系统

        try:
            await redis_cache_service.init()
            logger.info("✓ 多级缓存系统初始化成功 (L1内存 + L2 Redis)")
        except Exception as cache_err:
            logger.warning(f"⚠ 缓存系统初始化失败（将使用纯内存模式）: {cache_err}")

        # 4. 初始化所有连接池（DB/Redis/Milvus/Neo4j）
        try:
            await pool_manager.initialize_all()
        except Exception as pool_err:
            logger.warning(f"⚠ 连接池初始化失败: {pool_err}")
        
        # 4.1 初始化 Neo4j 异步连接（知识图谱使用）
        try:
            from app.repositories.graph.neo4j_connector import neo4j_manager
            connected = await neo4j_manager.connector.connect()
            if connected:
                logger.info("✓ Neo4j 异步连接就绪")
            else:
                logger.warning("⚠ Neo4j 异步连接未建立")
        except Exception as neo_err:
            logger.warning(f"⚠ Neo4j 异步连接初始化失败: {neo_err}")
        
        # 5. 预热关键缓存（可选）
        try:
            warm_stats = await cache_warmer.warm_all(concurrent_limit=3)
            if warm_stats.get("enabled"):
                logger.info(
                    f"✓ 缓存预热完成 | "
                    f"成功={warm_stats['success']} "
                    f"失败={warm_stats['failed']} "
                    f"跳过={warm_stats['skipped']}"
                )
        except Exception as warm_err:
            logger.warning(f"⚠ 缓存预热失败: {warm_err}")
        
        elapsed = time.time() - start_time
        # 初始化 ELK 日志推送
        global _elk_handler
        from app.core.logger import setup_logging
        elk_config = {
            "enabled": settings.elk.enabled,
            "host": settings.elk.logstash_host,
            "port": settings.elk.logstash_port,
            "index_name": settings.elk.index_name,
            "environment": settings.elk.environment,
            "timeout": settings.elk.logstash_timeout,
            "batch_size": settings.elk.batch_size,
            "flush_interval": settings.elk.flush_interval,
        }
        _elk_handler = setup_logging(
            level=logging.DEBUG if settings.debug else logging.INFO,
            json_logs=settings.is_production,
            elk_config=elk_config,
        )
        if _elk_handler:
            logger.info("✓ ELK 日志推送已启用")
        else:
            logger.info("ℹ ELK 日志推送未启用（ELK_ENABLED=false）")
        logger.info("=" * 60)
        logger.info(f"✅ 服务启动完成！耗时: {elapsed:.2f}秒")
        
    except Exception as e:
        logger.error(f"✗ 启动时初始化失败: {e}", exc_info=True)
        logger.warning("部分功能可能不可用，但服务将继续运行")


@app.on_event("shutdown")
async def shutdown_event():
    """
    应用关闭事件
    
    优雅关闭所有连接和资源，确保数据不丢失
    """
    logger.info("\nKQuery Pro API 服务开始关闭...")
    
    try:
        # 关闭所有外部服务连接
        await pool_manager.shutdown()

        # 关闭 ELK Logstash Handler
        global _elk_handler
        if _elk_handler:
            _elk_handler.close()
            _elk_handler = None
            logger.info("✓ ELK Logstash Handler 已关闭")

        logger.info("=" * 60)
        logger.info("👋 KQuery Pro API 已安全关闭")
        
    except Exception as e:
        logger.error(f"关闭过程出错: {e}")


@app.get("/health")
async def health_check():
    """健康检查接口（兼容旧版）"""
    return {
        "status": "healthy",
        "service": "KQuery Pro API",
        "version": "2.0.0"
    }


@app.get("/")
async def root():
    """根路径接口"""
    return {
        "message": "欢迎使用 KQuery Pro 知识库查询服务",
        "version": "2.0.0",
        "docs": "/docs",
        "health": "/health"
    }