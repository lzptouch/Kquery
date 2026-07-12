"""
连接池配置和优化

管理数据库、Redis、Milvus等外部服务的连接池
确保连接复用，避免频繁创建/销毁连接的开销
"""

from typing import Optional, Dict, Any
import asyncio

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool, QueuePool, StaticPool
from sqlalchemy import event, text

from app.core.settings import get_settings
from app.core.logger import get_logger


logger = get_logger(__name__)
settings = get_settings()


# ==================== 数据库连接池 ====================

def create_optimized_engine(
    db_url: str,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_recycle: int = 3600,
    pool_pre_ping: bool = True,
    echo_sql: bool = False,
    **kwargs
):
    """
    创建优化的异步SQLAlchemy引擎
    
    针对生产环境调优的连接池配置：
    - 连接复用（避免每次请求都新建连接）
    - 连接回收（防止长时间占用）
    - 健康检查（自动检测断开的连接）
    
    Args:
        db_url: 数据库URL（asyncpg://... 或 mysql+aiomysql://...）
        pool_size: 池中常驻连接数（建议=CPU核心数*2）
        max_overflow: 超出池大小的最大额外连接数
        pool_recycle: 连接回收时间（秒），防止长时间空闲被MySQL/PG断开
        pool_pre_ping: 是否在获取连接时先ping检测（推荐开启）
        echo_sql: 是否输出SQL日志（仅开发环境使用）
        
    Returns:
        配置好的AsyncEngine实例
    """
    engine = create_async_engine(
        db_url,
        poolclass=QueuePool,  # 使用队列连接池
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=30,     # 获取连接超时时间（秒）
        pool_recycle=pool_recycle,
        pool_pre_ping=pool_pre_ping,
        echo=echo_sql,
        # 异步引擎特定设置
        future=True,         # 使用2.0风格的API
        
        **kwargs
    )
    
    # 注册连接事件监听（可选的性能监控）
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        """针对SQLite的特殊设置"""
        if "sqlite" in str(db_url):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")  # WAL模式提高并发
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA cache_size=-64000")  # 64MB缓存
            cursor.close()
    
    logger.info(
        f"数据库引擎初始化 | PoolSize={pool_size}, "
        f"MaxOverflow={max_overflow}, PrePing={pool_pre_ping}"
    )
    
    return engine


def create_session_factory(engine) -> async_sessionmaker:
    """
    创建会话工厂
    
    Args:
        engine: SQLAlchemy异步引擎
        
    Returns:
        AsyncSession工厂函数
    """
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,  # 重要：异步模式下必须为False！否则访问已提交对象的属性会报错
        autoflush=False,        # 手动控制flush时机
        autocommit=False        # 显式事务管理
    )
    
    return session_factory


# ==================== Redis连接池配置 ====================

class RedisConnectionManager:
    """
    Redis连接管理器
    
    管理Redis连接的生命周期，支持：
    - 连接池化（减少TCP开销）
    - 自动重连
    - 多个独立连接（用于不同用途：缓存、限流、Pub/Sub等）
    """
    
    _pools: Dict[str, Any] = {}
    _connections: Dict[str, Any] = {}
    
    @classmethod
    def get_pool(
        cls,
        name: str = "default",
        max_connections: int = 20,
        decode_responses: bool = True
    ):
        """
        获取或创建Redis连接池
        
        Args:
            name: 连接池标识名（不同用途用不同名字）
            max_connections: 最大连接数
            decode_responses: 是否自动解码响应
            
        Returns:
            Redis连接池实例
        """
        if name in cls._pools:
            return cls._pools[name]
        
        try:
            import redis.asyncio as redis
            
            pool = redis.ConnectionPool.from_url(
                settings.redis_url,
                max_connections=max_connections,
                decode_responses=decode_responses,
                # 连接健康检查
                health_check_interval=30,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
            )
            
            cls._pools[name] = pool
            logger.info(f"✓ Redis连接池创建成功 | Name: {name}, MaxConn: {max_connections}")
            
            return pool
            
        except Exception as e:
            logger.error(f"✗ Redis连接池创建失败 | Name: {name} | 错误: {e}")
            raise
    
    @classmethod
    def get_connection(cls, name: str = "default"):
        """
        从指定连接池获取一个连接
        
        使用后记得归还（或使用上下文管理器）
        
        Args:
            name: 连接池名称
            
        Returns:
            Redis客户端实例
        """
        if name in cls._connections:
            return cls._connections[name]
        
        pool = cls.get_pool(name)
        client = redis.Redis(connection_pool=pool)
        cls._connections[name] = client
        return client
    
    @classmethod
    async def close_all(cls):
        """关闭所有连接池和连接"""
        for name, conn in cls._connections.items():
            try:
                await conn.close()
                logger.debug(f"✓ Redis连接关闭: {name}")
            except Exception as e:
                logger.error(f"✗ Redis连接关闭失败: {name} | {e}")
        
        for name, pool in cls._pools.items():
            try:
                await pool.disconnect()
                logger.debug(f"✓ Redis连接池关闭: {name}")
            except Exception as e:
                logger.error(f"✗ Redis连接池关闭失败: {name} | {e}")
        
        cls._connections.clear()
        cls._pools.clear()
        logger.info("所有Redis资源已释放")


# ==================== Milvus连接管理 ====================

class MilvusConnectionManager:
    """
    Milvus向量数据库连接管理器
    
    Milvus本身有内部连接池，这里主要管理：
    - 连接参数统一
    - 自动重连逻辑
    - 连接状态监控
    """
    
    _connected = False
    _connection_params = {}
    
    @classmethod
    def get_connection(cls):
        """
        获取Milvus连接（单例模式）
        
        Returns:
            Milvus连接对象
        """
        from pymilvus import connections, utility
        
        if cls._connected:
            return connections
        
        # 构建连接参数
        params = {
            "alias": "default",
            "host": settings.milvus_host,
            "port": settings.milvus_port,
        }
        
        # 可选认证
        if settings.milvus_user and settings.milvus_password:
            params["user"] = settings.milvus_user
            params["password"] = settings.milvus_password
        
        try:
            connections.connect(**params)
            cls._connected = True
            cls._connection_params = params
            
            # 测试连接
            server_version = utility.get_server_version()
            logger.info(f"✓ Milvus连接成功 | 版本: {server_version}")
            
            return connections
            
        except Exception as e:
            logger.error(f"✗ Milvus连接失败: {e}")
            raise
    
    @classmethod
    async def check_connection(cls) -> bool:
        """检查连接是否正常"""
        if not cls._connected:
            return False
        
        try:
            from pymilvus import utility
            utility.get_server_version()
            return True
        except Exception:
            cls._connected = False
            logger.warning("⚠ Milvus连接已断开")
            return False
    
    @classmethod
    def disconnect(cls):
        """断开连接"""
        try:
            from pymilvus import connections
            if cls._connected:
                connections.disconnect(alias="default")
                cls._connected = False
                logger.info("✓ Milvus连接已关闭")
        except Exception as e:
            logger.error(f"✗ Milvus断连失败: {e}")


# ==================== Neo4j连接管理（预留） ====================

class Neo4jConnectionManager:
    """
    Neo4j图数据库连接管理器（用于知识图谱功能）
    """
    
    _driver = None
    
    @classmethod
    def get_driver(cls):
        """获取Neo4j驱动实例"""
        if cls._driver is not None:
            return cls._driver
        
        try:
            from neo4j import GraphDatabase
            
            uri = settings.neo4j_uri or "bolt://localhost:7687"
            
            auth = None
            if settings.neo4j_user and settings.neo4j_password:
                auth = (settings.neo4j_user, settings.neo4j_password)
            
            cls._driver = GraphDatabase.driver(uri, auth=auth)
            
            # 测试连接
            with cls._driver.session() as session:
                result = session.run("RETURN 1 AS test")
                result.single()
            
            logger.info(f"✓ Neo4j连接成功 | URI: {uri}")
            return cls._driver
            
        except ImportError:
            logger.warning("Neo4j驱动未安装 (pip install neo4j)")
            return None
        except Exception as e:
            logger.error(f"✗ Neo4j连接失败: {e}")
            return None
    
    @classmethod
    def close(cls):
        """关闭Neo4j连接"""
        if cls._driver is not None:
            cls._driver.close()
            cls._driver = None
            logger.info("✓ Neo4j连接已关闭")


# ==================== 统一的连接管理入口 ====================

class ConnectionPoolManager:
    """
    全局连接池管理器
    
    提供统一的接口管理所有外部服务的连接：
    - 初始化所有连接池
    - 健康状态检查
    - 优雅关闭
    - 连接统计信息
    """
    
    _initialized = False
    
    @classmethod
    async def initialize_all(cls):
        """
        初始化所有连接池
        
        应该在应用启动时调用一次
        """
        if cls._initialized:
            return
        
        logger.info("=" * 50)
        logger.info("开始初始化所有连接...")
        
        # 1. 数据库连接池（由settings管理，这里只做日志确认）
        logger.info(f"  [DB] URL: {settings.database_url[:20]}... (已通过engine初始化)")
        
        # 2. Redis连接池
        try:
            RedisConnectionManager.get_pool(name="cache", max_connections=10)
            RedisConnectionManager.get_pool(name="ratelimit", max_connections=5)
            RedisConnectionManager.get_pool(name="session", max_connections=5)
            logger.info("  [Redis] ✓ 缓存 + 限流 + 会话 连接池就绪")
        except Exception as e:
            logger.error(f"  [Redis] ✗ 初始化失败: {e}")
        
        # 3. Milvus连接
        try:
            MilvusConnectionManager.get_connection()
            logger.info("  [Milvus] ✓ 连接就绪")
        except Exception as e:
            logger.error(f"  [Milvus] ✗ 连接失败: {e}")
        
        # 4. Neo4j连接（可选）
        try:
            driver = Neo4jConnectionManager.get_driver()
            if driver:
                logger.info("  [Neo4j] ✓ 连接就绪")
            else:
                logger.warning("  [Neo4j] ⊘ 未配置或不可用（知识图谱功能受限）")
        except Exception as e:
            logger.warning(f"  [Neo4j] ✗ 连接跳过: {e}")
        
        cls._initialized = True
        logger.info("=" * 50)
        logger.info("所有连接池初始化完成\n")
    
    @classmethod
    async def health_check(cls) -> Dict[str, bool]:
        """
        执行全面的连接健康检查
        
        Returns:
            各服务连接状态字典：
            {
                "database": True/False,
                "redis": True/False,
                "milvus": True/False,
                "neo4j": True/False
            }
        """
        results = {"database": False, "redis": False, "milvus": False, "neo4j": False}
        
        # 数据库检查
        try:
            async with settings.async_session_maker() as session:
                await session.execute(text("SELECT 1"))
            results["database"] = True
        except Exception as e:
            logger.error(f"健康检查 - DB失败: {e}")
        
        # Redis检查
        try:
            redis_client = RedisConnectionManager.get_connection("cache")
            await redis_client.ping()
            results["redis"] = True
        except Exception as e:
            logger.error(f"健康检查 - Redis失败: {e}")
        
        # Milvus检查
        results["milvus"] = await MilvusConnectionManager.check_connection()
        
        # Neo4j检查
        try:
            driver = Neo4jConnectionManager.get_driver()
            if driver:
                with driver.session() as session:
                    session.run("RETURN 1").single()
                results["neo4j"] = True
        except Exception:
            pass
        
        all_healthy = all(results.values())
        status = "HEALTHY" if all_healthy else "DEGRADED"
        
        logger.info(f"连接池健康检查完成 | 状态: {status} | 详情: {results}")
        
        return results
    
    @classmethod
    async def shutdown(cls):
        """
        优雅关闭所有连接
        
        应用退出前调用，确保：
        - 所有未完成的事务回滚或提交
        - 所有连接归还到连接池
        - 连接池中的连接正确关闭
        - 不丢失数据
        """
        logger.info("开始关闭所有连接...")
        
        try:
            # 关闭数据库引擎
            if hasattr(settings, 'async_engine'):
                await settings.async_engine.dispose()
                logger.info("  [DB] ✓ 引擎已关闭")
            
            # 关闭Redis连接
            await RedisConnectionManager.close_all()
            logger.info("  [Redis] ✓ 已关闭")
            
            # 关闭Milvus
            MilvusConnectionManager.disconnect()
            logger.info("  [Milvus] ✓ 已关闭")
            
            # 关闭Neo4j
            Neo4jConnectionManager.close()
            logger.info("  [Neo4j] ✓ 已关闭")
            
            cls._initialized = False
            logger.info("=" * 50)
            logger.info("所有连接已安全关闭\n")
            
        except Exception as e:
            logger.error(f"关闭连接时出错: {e}", exc_info=True)


# 全局实例
pool_manager = ConnectionPoolManager()
