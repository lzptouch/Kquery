"""
Milvus 连接器
提供 Milvus 向量数据库的连接管理和客户端实例
"""

from pymilvus import connections, utility
from app.core.settings import get_settings
from app.core.logger import get_logger

settings = get_settings()

logger = get_logger(__name__)

class MilvusConnector:
    """Milvus 连接器"""
    
    def __init__(self):
        self.alias = "default"
        self.connected = False
    
    def connect(self):
        """建立 Milvus 连接 (同步)"""
        try:
            # 使用配置中的参数建立连接
            connect_kwargs = {
                "alias": self.alias,
                "host": settings.database.milvus_host,
                "port": settings.database.milvus_port,
                "user": settings.database.milvus_user or "root",
                "password": settings.database.milvus_password or "",
                "timeout": 10.0, # 设置较短的连接超时
            }
            
            import os
            milvus_database = os.getenv("MILVUS_DATABASE")
            if milvus_database:
                connect_kwargs["db_name"] = milvus_database
            
            milvus_secure = os.getenv("MILVUS_SECURE")
            if milvus_secure:
                connect_kwargs["secure"] = milvus_secure.lower() in ('true', '1', 'yes')
            
            # 先检查是否已经连接，避免重复操作
            from pymilvus import connections
            if not connections.has_connection(self.alias):
                connections.connect(**connect_kwargs)
            
            self.connected = True
            logger.info(f"Milvus 连接成功: {settings.database.milvus_host}:{settings.database.milvus_port}")
        except Exception as e:
            logger.error(f"Milvus 连接失败: {e}")
            self.connected = False
            # 不要在这里抛出异常，让业务层处理
    
    async def async_connect(self):
        """异步建立 Milvus 连接"""
        import asyncio
        if not self.connected:
            await asyncio.to_thread(self.connect)
        return self.connected
    
    def disconnect(self):
        """关闭 Milvus 连接"""
        try:
            connections.disconnect(self.alias)
            self.connected = False
            logger.info("Milvus 连接已断开")
        except Exception as e:
            logger.error(f"断开 Milvus 连接时出错: {e}")
    
    def get_connection(self):
        """获取连接实例"""
        if not self.connected:
            self.connect()
        # 返回别名，因为 pymilvus 通常使用别名来引用连接
        return self.alias
    
    def has_collection(self, collection_name: str) -> bool:
        """检查集合是否存在"""
        try:
            return utility.has_collection(collection_name, using=self.alias)
        except Exception as e:
            logger.error(f"检查集合 {collection_name} 时出错: {e}")
            return False
    
    def list_collections(self) -> list:
        """列出所有集合"""
        try:
            return utility.list_collections(using=self.alias)
        except Exception as e:
            logger.error(f"列出集合时出错: {e}")
            return []


# 全局连接器实例
milvus_connector = MilvusConnector()


def get_milvus_connection(alias: str = "default"):
    """获取 Milvus 连接实例"""
    if not milvus_connector.connected:
        milvus_connector.connect()
    # 在 pymilvus 中，连接通常通过别名进行操作
    return alias


def get_milvus_connector():
    """获取 Milvus 连接器实例"""
    if not milvus_connector.connected:
        milvus_connector.connect()
    return milvus_connector

def test_milvus_connection() -> bool:
    """测试 Milvus 连接"""
    try:
        if not milvus_connector.connected:
            milvus_connector.connect()
        # 尝试列出集合作为连通性测试
        utility.list_collections(using=milvus_connector.alias)
        return True
    except Exception as e:
        logger.error(f"Milvus 连接测试失败: {e}")
        return False