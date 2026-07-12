"""
Milvus连接器
简化的数据库连接管理，基于Kquery配置
"""
import os
import sys
from typing import Optional, Dict, Any
from pathlib import Path

from pymilvus import connections, utility, Collection
from app.core.settings import get_settings
from app.core.logger import get_logger

settings = get_settings()

logger = get_logger(__name__)


class MilvusConnector:
    """Milvus连接器"""
    
    def __init__(self):
        self._connected = False
        self._connection_name = "kquery_connection"
        
    def connect(self) -> bool:
        """连接到Milvus"""
        try:
            if self._connected:
                return True
                
            # 检查是否已经存在连接
            if self._connection_name in connections.list_connections():
                self._connected = True
                return True
            
            # 建立新连接
            connections.connect(
                alias=self._connection_name,
                host=settings.database.milvus_host,
                port=settings.database.milvus_port,
                user=settings.database.milvus_user or "root",
                password=settings.database.milvus_password or "",
            )
            
            self._connected = True
            logger.info(f"成功连接到Milvus: {settings.database.milvus_host}:{settings.database.milvus_port}")
            return True
            
        except Exception as e:
            logger.error(f"连接到Milvus失败: {e}")
            self._connected = False
            return False
    
    def disconnect(self) -> bool:
        """断开连接"""
        try:
            if self._connection_name in connections.list_connections():
                connections.disconnect(self._connection_name)
            self._connected = False
            logger.info("已断开Milvus连接")
            return True
        except Exception as e:
            logger.error(f"断开Milvus连接失败: {e}")
            return False
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._connected and self._connection_name in connections.list_connections()
    
    def get_connection_name(self) -> Optional[str]:
        """获取连接名称"""
        return self._connection_name if self.is_connected() else None
    
    def has_collection(self, collection_name: str) -> bool:
        """检查集合是否存在"""
        try:
            if not self.is_connected():
                return False
            return utility.has_collection(collection_name, using=self._connection_name)
        except Exception as e:
            logger.error(f"检查集合存在性失败: {collection_name}, 错误: {e}")
            return False
    
    def get_collection(self, collection_name: str) -> Optional[Collection]:
        """获取集合实例"""
        try:
            if not self.is_connected() or not self.has_collection(collection_name):
                return None
            return Collection(collection_name, using=self._connection_name)
        except Exception as e:
            logger.error(f"获取集合失败: {collection_name}, 错误: {e}")
            return None


# 单例实例
_connector: Optional[MilvusConnector] = None


def get_milvus_connector() -> MilvusConnector:
    """获取Milvus连接器单例"""
    global _connector
    if _connector is None:
        _connector = MilvusConnector()
        _connector.connect()
    return _connector


def get_milvus_connection() -> Optional[str]:
    """获取Milvus连接名称"""
    connector = get_milvus_connector()
    return connector.get_connection_name() if connector.is_connected() else None