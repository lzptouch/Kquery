"""
Neo4j图数据库连接器和管理器

提供Neo4j数据库的连接、查询、事务管理等核心功能。
支持异步操作、连接池、健康检查等企业级特性。

核心功能：
1. 异步Neo4j驱动封装
2. 连接池管理和复用
3. Schema自动初始化（约束和索引）
4. 健康检查和状态监控
5. 事务管理（支持读写分离）
6. 查询性能统计

使用示例：
    from app.repositories.graph.neo4j_connector import neo4j_manager
    
    # 执行Cypher查询
    result = await neo4j_manager.execute_query(
        "MATCH (n:Entity) RETURN n LIMIT 10"
    )
    
    # 使用事务
    async with neo4j_manager.transaction() as tx:
        await tx.run("CREATE (e:Entity {name: $name})", name="Test")
    
作者：KQuery Pro Team
版本：1.0.0
更新：2026-04-11
"""

from __future__ import annotations

import asyncio
import time
import logging
from typing import (
    Any, Dict, List, Optional, AsyncIterator, 
    Union, Callable, Awaitable
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

try:
    from neo4j import AsyncGraphDatabase, AsyncDriver, AsyncSession, AsyncTransaction
    from neo4j.exceptions import (
        ServiceUnavailable, 
        AuthError,
        ConfigurationError,
        TransientError,
        ClientError
    )
    NEO4J_AVAILABLE = True
except ImportError:
    # 如果未安装neo4j驱动，提供占位符类以便开发时不会报错
    NEO4J_AVAILABLE = False
    AsyncDriver = None
    AsyncSession = None
    AsyncTransaction = None
    
    class ServiceUnavailable(Exception):
        pass
    
    class AuthError(Exception):
        pass


logger = logging.getLogger(__name__)


@dataclass
class QueryStatistics:
    """查询统计信息"""
    query_count: int = 0
    total_time_ms: float = 0.0
    avg_time_ms: float = 0.0
    slow_queries: List[Dict[str, Any]] = field(default_factory=list)
    errors: int = 0
    
    def record(self, query_time_ms: float, query: str, is_slow: bool = False):
        """记录一次查询"""
        self.query_count += 1
        self.total_time_ms += query_time_ms
        self.avg_time_ms = self.total_time_ms / self.query_count
        
        if is_slow or query_time_ms > 1000:  # 超过1秒视为慢查询
            self.slow_queries.append({
                "query": query[:200],  # 截断长查询
                "time_ms": round(query_time_ms, 2),
                "timestamp": time.time()
            })
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "query_count": self.query_count,
            "total_time_ms": round(self.total_time_ms, 2),
            "avg_time_ms": round(self.avg_time_ms, 2),
            "slow_query_count": len(self.slow_queries),
            "errors": self.errors
        }


@dataclass
class ConnectionConfig:
    """Neo4j连接配置"""
    uri: str = "bolt://localhost:7687"  # Bolt协议端口
    username: str = "neo4j"
    password: str = "password"
    database: str = "neo4j"  # 数据库名称（Neo4j 4.x+支持多数据库）
    
    # 连接池配置
    max_connection_pool_size: int = 20       # 最大连接数
    connection_timeout: int = 30             # 连接超时（秒）
    max_transaction_retry_time: int = 30     # 事务最大重试时间（秒）
    
    # 加密配置
    encrypted: bool = False                  # 是否使用TLS加密
    trust: str = "TRUST_SYSTEM_CA_SIGNED_CERTIFICATES"  # 信任策略
    
    @classmethod
    def from_settings(cls) -> "ConnectionConfig":
        """从应用设置创建配置"""
        try:
            from app.core.settings import get_settings
            settings = get_settings()
            
            config = cls(
                uri=getattr(settings, 'neo4j_uri', 'bolt://localhost:7687'),
                username=getattr(settings, 'neo4j_user', 'neo4j'),
                password=getattr(settings, 'neo4j_password', 'password'),
                database=getattr(settings, 'neo4j_database', 'neo4j'),
                max_connection_pool_size=getattr(settings, 'neo4j_pool_size', 20),
                encrypted=getattr(settings, 'neo4j_encrypted', False)
            )
            
            if not hasattr(settings, 'neo4j_uri'):
                logger.warning("⚠️ 未检测到Neo4j配置，使用默认值。请在settings.py中配置NEO4J_URI等参数")
            
            return config
        except Exception as e:
            logger.warning(f"无法从settings加载Neo4j配置，使用默认值: {e}")
            return cls()


if NEO4J_AVAILABLE:

    class Neo4jConnector:
        """
        Neo4j异步连接器
        
        提供完整的Neo4j数据库访问能力，包括：
        - 异步查询执行
        - 事务管理
        - 连接池管理
        - 自动重连机制
        - 查询性能统计
        
        线程安全，可在整个应用中共享使用。
        """
        
        def __init__(self, config: Optional[ConnectionConfig] = None):
            """
            初始化连接器
            
            Args:
                config: 连接配置，如果为None则从应用设置加载
            """
            self.config = config or ConnectionConfig.from_settings()
            self._driver: Optional[AsyncDriver] = None
            self._is_connected = False
            self.statistics = QueryStatistics()
            self._lock = asyncio.Lock()
            
            logger.info(f"🔗 Neo4j连接器初始化 | URI: {self.config.uri}")
        
        async def connect(self) -> bool:
            """
            建立到Neo4j服务器的连接
            
            Returns:
                是否成功建立连接
            """
            if self._is_connected and self._driver:
                return True
            
            async with self._lock:
                try:
                    start_time = time.time()
                    
                    self._driver = AsyncGraphDatabase.driver(
                        self.config.uri,
                        auth=(
                            self.config.username,
                            self.config.password
                        ),
                        max_connection_pool_size=self.config.max_connection_pool_size,
                        connection_timeout=self.config.connection_timeout,
                        encrypted=self.config.encrypted,
                        user_agent="KQuery-Pro/2.0"
                    )
                    
                    # 验证连接是否可用
                    await self._driver.verify_connectivity()
                    
                    elapsed = time.time() - start_time
                    self._is_connected = True
                    
                    logger.info(f"✅ Neo4j连接成功 | 耗时: {elapsed:.2f}s | "
                               f"池大小: {self.config.max_connection_pool_size}")
                    
                    return True
                    
                except AuthError as e:
                    logger.error(f"❌ Neo4j认证失败: {e}")
                    self._is_connected = False
                    raise ConnectionError("Neo4j认证失败，请检查用户名和密码") from e
                    
                except ServiceUnavailable as e:
                    logger.error(f"❌ Neo4j服务不可用: {e}")
                    self._is_connected = False
                    raise ConnectionError("Neo4j服务器不可用，请检查URI和服务状态") from e
                    
                except Exception as e:
                    logger.error(f"❌ Neo4j连接失败: {e}")
                    self._is_connected = False
                    raise
        
        async def close(self):
            """关闭所有连接"""
            if self._driver:
                try:
                    await self._driver.close()
                    self._is_connected = False
                    self._driver = None
                    logger.info("✓ Neo4j连接已关闭")
                except Exception as e:
                    logger.error(f"关闭Neo4j连接出错: {e}")
        
        async def verify_connectivity(self) -> Dict[str, Any]:
            """
            验证连接健康状态
            
            Returns:
                健康检查结果字典
            """
            result = {
                "connected": False,
                "server_info": None,
                "database_accessible": False,
                "latency_ms": None
            }
            
            if not self._driver:
                return result
            
            try:
                start_time = time.time()
                
                # 验证基础连通性
                server_info = await self._driver.verify_connectivity()
                
                latency = (time.time() - start_time) * 1000
                
                # 验证数据库可访问性
                session = self._driver.session(database=self.config.database)
                try:
                    await session.run("RETURN 1 AS test")
                    database_ok = True
                finally:
                    await session.close()
                
                result.update({
                    "connected": True,
                    "server_info": server_info,
                    "database_accessible": database_ok,
                    "latency_ms": round(latency, 2)
                })
                
            except Exception as e:
                logger.warning(f"Neo4j健康检查失败: {e}")
                result["error"] = str(e)
            
            return result
        
        @asynccontextmanager
        async def session(self, **kwargs) -> AsyncIterator[AsyncSession]:
            """
            获取数据库会话（上下文管理器）
            
            自动处理会话的获取和关闭，推荐使用方式：
            
            Usage:
                async with connector.session() as session:
                    result = await session.run("...")
                    records = await result.data()
            """
            if not self._driver:
                raise RuntimeError("Neo4j尚未连接，请先调用connect()")
            
            default_kwargs = {
                "database": self.config.database,
                "default_access_mode": "READ"  # 默认读模式
            }
            default_kwargs.update(kwargs)
            
            sess = self._driver.session(**default_kwargs)
            try:
                yield sess
            finally:
                await sess.close()
        
        @asynccontextmanager
        async def transaction(
            self, 
            access_mode: str = "WRITE"
        ) -> AsyncIterator[AsyncTransaction]:
            """
            获取事务上下文
            
            Args:
                access_mode: 访问模式 ("READ" 或 "WRITE")
            
            Usage:
                async with connector.transaction() as tx:
                    await tx.run("CREATE ...")
                    # 所有操作在同一个事务中执行
            """
            if not self._driver:
                raise RuntimeError("Neo4j尚未连接，请先调用connect()")
            
            sess = self._driver.session(
                database=self.config.database,
                default_access_mode=access_mode
            )
            tx = None
            try:
                async with sess.begin_transaction() as tx_obj:
                    yield tx_obj
            finally:
                await sess.close()
        
        async def execute_query(
            self,
            cypher: str,
            parameters: Optional[Dict[str, Any]] = None,
            access_mode: str = "READ",
            timeout: Optional[float] = None
        ) -> List[Dict[str, Any]]:
            """
            执行Cypher查询并返回结果列表
            
            这是最常用的查询方法，适用于大多数场景。
            
            Args:
                cypher: Cypher查询语句
                parameters: 查询参数（防SQL注入）
                access_mode: "READ" 或 "WRITE"
                timeout: 查询超时时间（秒），None表示不限制
            
            Returns:
                查询结果列表，每条记录是一个字典
            
            Example:
                # 查找实体
                results = await connector.execute_query(
                    "MATCH (e:Entity {name: $name}) RETURN e",
                    {"name": "张三"}
                )
                
                # 创建节点
                await connector.execute_query(
                    "CREATE (e:Entity $props)",
                    {"props": {"name": "新实体", "type": "Person"}},
                    access_mode="WRITE"
                )
            """
            if not self._driver:
                raise RuntimeError("Neo4j尚未连接")
            
            start_time = time.time()
            parameters = parameters or {}
            
            try:
                async with self.session(default_access_mode=access_mode) as sess:
                    if timeout:
                        result = await sess.run(
                            cypher, 
                            parameters, 
                            timeout=timeout * 1000  # Neo4j使用毫秒
                        )
                    else:
                        result = await sess.run(cypher, parameters)
                    
                    records = await result.data()
                    
                elapsed = (time.time() - start_time) * 1000
                is_slow = elapsed > 1000  # 超过1秒标记为慢查询
                self.statistics.record(elapsed, cypher, is_slow)
                
                if is_slow:
                    logger.warning(f"⚠️ 慢查询检测 ({elapsed:.0f}ms): {cypher[:100]}...")
                
                return records
                
            except TransientError as e:
                # 临时错误（如网络波动），可以重试
                self.statistics.errors += 1
                logger.warning(f"Neo4j临时错误(可能需要重试): {e}")
                raise
                
            except ClientError as e:
                # 客户端错误（如语法错误）
                self.statistics.errors += 1
                logger.error(f"Neo4j查询错误: {e}\nCypher: {cypher[:200]}")
                raise ValueError(f"Cypher查询错误: {e}") from e
                
            except Exception as e:
                self.statistics.errors += 1
                logger.error(f"Neo4j执行异常: {e}", exc_info=True)
                raise
        
        async def execute_write(
            self,
            cypher: str,
            parameters: Optional[Dict[str, Any]] = None,
            batch_size: int = 1000
        ) -> Dict[str, Any]:
            """
            执行写入操作（优化的批量写入）
            
            对于批量数据插入，建议使用UNWIND语句配合此方法。
            
            Args:
                cypher: Cypher写入语句
                parameters: 参数字典
                batch_size: 批量大小（当参数包含列表时有效）
            
            Returns:
                写入统计信息
            """
            stats = await self.execute_query(
                cypher,
                parameters,
                access_mode="WRITE"
            )
            
            return {
                "rows_affected": len(stats),
                "statistics": stats[0] if stats else {}
            }
        
        async def initialize_schema(self):
            """
            初始化知识图谱Schema
            
            创建必要的约束和索引以确保数据完整性和查询性能。
            应该在首次部署或数据库重置后调用。
            """
            logger.info("🔄 开始初始化Neo4j Schema（约束和索引）...")
            
            schema_definitions = [
                # ===== 唯一约束（确保数据唯一性）=====
                "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (e:Entity) REQUIRE e.id IS UNIQUE",
                
                # ===== 组合唯一约束 =====
                "CREATE CONSTRAINT entity_name_type_unique IF NOT EXISTS FOR (e:Entity) REQUIRE (e.name, e.type) IS UNIQUE",
                
                "CREATE CONSTRAINT relation_id_unique IF NOT EXISTS FOR ()-[r:RELATED_TO]-() REQUIRE r.id IS UNIQUE",
                
                # ===== 文档元节点约束 =====
                "CREATE CONSTRAINT document_id_unique IF NOT EXISTS FOR (d:DocumentNode) REQUIRE d.doc_id IS UNIQUE",
                "CREATE CONSTRAINT page_id_unique IF NOT EXISTS FOR (p:Page) REQUIRE p.id IS UNIQUE",
                "CREATE CONSTRAINT table_id_unique IF NOT EXISTS FOR (t:Table) REQUIRE t.id IS UNIQUE",
                "CREATE CONSTRAINT image_id_unique IF NOT EXISTS FOR (i:Image) REQUIRE i.id IS UNIQUE",
                "CREATE CONSTRAINT paragraph_id_unique IF NOT EXISTS FOR (p:Paragraph) REQUIRE p.id IS UNIQUE",
                
                # ===== 元关系唯一约束 =====
                "CREATE CONSTRAINT contains_id_unique IF NOT EXISTS FOR ()-[r:CONTAINS]-() REQUIRE r.id IS UNIQUE",
                "CREATE CONSTRAINT has_row_id_unique IF NOT EXISTS FOR ()-[r:HAS_ROW]-() REQUIRE r.id IS UNIQUE",
                "CREATE CONSTRAINT describes_id_unique IF NOT EXISTS FOR ()-[r:DESCRIBES]-() REQUIRE r.id IS UNIQUE",
                
                # ===== 索引（加速常用查询）=====
                # 实体名称索引（用于模糊搜索）
                "CREATE INDEX entity_name_text_index IF NOT EXISTS FOR (e:Entity) ON (e.name) OPTIONS {indexProvider: 'text-2.0'}",
                
                # 实体类型索引
                "CREATE INDEX entity_type_index IF NOT EXISTS FOR (e:Entity) ON (e.type)",
                
                # 实体名称前缀索引（用于自动补全）
                "CREATE INDEX entity_name_index IF NOT EXISTS FOR (e:Entity) ON (e.name)",
                
                # 关系类型索引
                "CREATE INDEX relation_type_index IF NOT EXISTS FOR ()-[r:RELATED_TO]-() ON (r.type)",
                
                # 新增元关系索引
                "CREATE INDEX contains_type_index IF NOT EXISTS FOR ()-[r:CONTAINS]-() ON (r.type)",
                "CREATE INDEX has_row_type_index IF NOT EXISTS FOR ()-[r:HAS_ROW]-() ON (r.type)",
                "CREATE INDEX describes_type_index IF NOT EXISTS FOR ()-[r:DESCRIBES]-() ON (r.type)",
                
                # 文档元节点索引
                "CREATE INDEX document_doc_id_index IF NOT EXISTS FOR (d:DocumentNode) ON (d.doc_id)",
                "CREATE INDEX page_doc_id_index IF NOT EXISTS FOR (p:Page) ON (p.doc_id)",
                "CREATE INDEX table_doc_id_index IF NOT EXISTS FOR (t:Table) ON (t.doc_id)",
                "CREATE INDEX image_doc_id_index IF NOT EXISTS FOR (i:Image) ON (i.doc_id)",
                "CREATE INDEX paragraph_doc_id_index IF NOT EXISTS FOR (p:Paragraph) ON (p.doc_id)",
                
                # 置信度索引（用于过滤低质量数据）
                "CREATE INDEX entity_confidence_index IF NOT EXISTS FOR (e:Entity) ON (e.confidence)",
                
                # 时间戳索引
                "CREATE INDEX entity_created_at_index IF NOT EXISTS FOR (e:Entity) ON (e.created_at)",
                
                # 全文搜索索引（如果支持）
                # CREATE FULLTEXT INDEX entity_fulltext IF NOT EXISTS 
                # FOR (e:Entity) ON EACH [e.name] OPTIONS {indexProvider: 'fulltext-1.0'}
            ]
            
            success_count = 0
            failed_statements = []
            
            for statement in schema_definitions:
                try:
                    await self.execute_query(statement, access_mode="WRITE")
                    success_count += 1
                    logger.debug(f"✓ Schema创建成功: {statement[:60]}...")
                    
                except Exception as e:
                    failed_statements.append({
                        "statement": statement[:100],
                        "error": str(e)
                    })
                    # 某些旧版Neo4j可能不支持某些特性，记录但不中断
                    logger.warning(f"Schema创建警告: {e} | Statement: {statement[:80]}")
            
            logger.info(f"✅ Schema初始化完成 | 成功: {success_count}/{len(schema_definitions)}")
            
            if failed_statements:
                logger.warning(f"有 {len(failed_statements)} 个语句未能执行（可能是版本兼容性问题）")
            
            return {
                "success": success_count,
                "failed": len(failed_statements),
                "details": failed_statements if failed_statements else None
            }
        
        def get_statistics(self) -> Dict[str, Any]:
            """获取连接器统计信息"""
            return {
                "is_connected": self._is_connected,
                "config_uri": self.config.uri,
                "config_database": self.config.database,
                "pool_size": self.config.max_connection_pool_size,
                **self.statistics.to_dict()
            }
        
        def __repr__(self) -> str:
            status = "已连接" if self._is_connected else "未连接"
            return f"<Neo4jConnector [{status}] {self.config.uri}/{self.config.database}>"

else:
    # Neo4j驱动未安装时的占位符类
    class Neo4jConnector:
        def __init__(self, config=None):
            logger.warning("⚠️ Neo4j Python驱动未安装，图谱功能不可用。请运行: uv add neo4j")
        
        async def connect(self):
            raise ImportError("请安装neo4j包: uv add neo4j")
        
        async def close(self):
            pass
        
        async def execute_query(self, *args, **kwargs):
            raise NotImplementedError()


# ==================== 全局单例管理器 ====================

class Neo4jManager:
    """
    Neo4j全局管理器
    
    在整个应用中共享单一的Neo4j连接实例，
    提供统一的管理接口。
    
    单例模式，通过get_instance()或直接导入ne4j_manager获取实例。
    """
    
    _instance: Optional[Neo4jManager] = None
    _connector: Optional[Neo4jConnector] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    @property
    def connector(self) -> Neo4jConnector:
        """获取底层连接器实例"""
        if self._connector is None:
            self._connector = Neo4jConnector()
        return self._connector
    
    @property
    def is_initialized(self) -> bool:
        """是否已初始化并连接"""
        return self._connector is not None and self._connector._is_connected
    
    async def initialize(
        self, 
        config: Optional[ConnectionConfig] = None,
        init_schema: bool = True
    ) -> bool:
        """
        初始化Neo4j连接
        
        Args:
            config: 自定义配置（可选）
            init_schema: 是否同时初始化Schema（创建约束和索引）
        
        Returns:
            是否初始化成功
        """
        try:
            if config:
                self._connector = Neo4jConnector(config)
            
            # 建立连接
            connected = await self.connector.connect()
            
            if not connected:
                logger.error("❌ 无法连接到Neo4j服务器")
                return False
            
            # 可选：初始化Schema
            if init_schema:
                try:
                    await self.connector.initialize_schema()
                except Exception as e:
                    logger.warning(f"Schema初始化失败（不影响连接）: {e}")
            
            logger.info("✅ Neo4j管理器初始化完成")
            return True
            
        except Exception as e:
            logger.error(f"❌ Neo4j初始化失败: {e}")
            return False
    
    async def shutdown(self):
        """关闭连接"""
        if self._connector:
            await self._connector.close()
            logger.info("✓ Neo4j管理器已关闭")
    
    async def health_check(self) -> Dict[str, Any]:
        """执行健康检查"""
        if not self._connector:
            return {
                "status": "not_configured",
                "message": "Neo4j未配置"
            }
        
        health = await self._connector.verify_connectivity()
        health["status"] = "healthy" if health.get("connected") else "unhealthy"
        health["statistics"] = self._connector.get_statistics()
        
        return health
    
    def get_connector(self) -> Neo4jConnector:
        """获取连接器（便捷方法）"""
        return self.connector


# 全局单例实例（模块级导出）
neo4j_manager = Neo4jManager()

# 便捷导出函数
async def get_neo4j_session():
    """获取Neo4j会话（便捷函数）"""
    return neo4j_manager.connector.session()

async def execute_cypher(cypher: str, params: dict = None):
    """执行Cypher查询（便捷函数）"""
    return await neo4j_manager.connector.execute_query(cypher, params)


__all__ = [
    'Neo4jConnector',
    'ConnectionConfig',
    'QueryStatistics',
    'Neo4jManager',
    'neo4j_manager',
    'get_neo4j_session',
    'execute_cypher'
]
