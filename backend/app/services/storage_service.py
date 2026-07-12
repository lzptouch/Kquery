"""
KQuery Pro 存储抽象层

提供统一的文件存储接口，支持本地文件系统和 MinIO 对象存储两种实现。
通过配置 STORAGE_TYPE=local|minio 切换存储后端。

设计原则：
- 抽象基类定义统一接口
- RAGService 无侵入：API 层负责从 MinIO 下载到临时目录再传入
- 向后兼容：STORAGE_TYPE 默认 local，无需修改配置即可工作
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import time

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from app.core.logger import get_logger
from app.core.settings import get_settings

logger = get_logger(__name__)
settings = get_settings()


class StorageService(ABC):
    """存储服务抽象基类"""

    @abstractmethod
    async def save(self, content: bytes, filename: str, metadata: Optional[dict] = None) -> str:
        """保存文件，返回存储路径/标识"""
        ...

    @abstractmethod
    async def get_local_path(self, filename: str) -> str:
        """获取文件的本地临时路径（供 RAGService 读取）"""
        ...

    @abstractmethod
    async def delete(self, filename: str) -> bool:
        """删除文件"""
        ...

    @abstractmethod
    async def exists(self, filename: str) -> bool:
        """检查文件是否存在"""
        ...


class LocalStorageService(StorageService):
    """本地文件系统实现 - 保持现有行为"""

    def __init__(self, upload_dir: str):
        self.upload_dir = upload_dir
        os.makedirs(self.upload_dir, exist_ok=True)
        logger.info(f"本地存储初始化完成，目录: {self.upload_dir}")

    async def save(self, content: bytes, filename: str, metadata: Optional[dict] = None) -> str:
        start = time.time()
        file_path = os.path.join(self.upload_dir, filename)
        with open(file_path, "wb") as f:
            f.write(content)
        elapsed = time.time() - start
        logger.info(f"本地保存文件完成: {filename} ({len(content)} bytes, {elapsed:.3f}s)")
        return file_path

    async def get_local_path(self, filename: str) -> str:
        file_path = os.path.join(self.upload_dir, filename)
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"本地文件不存在: {file_path}")
        return file_path

    async def delete(self, filename: str) -> bool:
        file_path = os.path.join(self.upload_dir, filename)
        if os.path.exists(file_path):
            os.unlink(file_path)
            logger.info(f"本地文件已删除: {file_path}")
            return True
        return False

    async def exists(self, filename: str) -> bool:
        return os.path.exists(os.path.join(self.upload_dir, filename))


class MinioStorageService(StorageService):
    """MinIO 对象存储实现"""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        region: str = "us-east-1",
    ):
        from minio import Minio
        from minio.error import S3Error

        self.endpoint = endpoint
        self.bucket = bucket
        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
            region=region,
        )
        self._ensure_bucket()
        logger.info(
            f"MinIO 存储初始化完成，端点: {endpoint}, 存储桶: {bucket}"
        )

    def _ensure_bucket(self):
        """确保存储桶存在，不存在则创建"""
        from minio.error import S3Error

        try:
            if not self.client.bucket_exists(self.bucket):
                self.client.make_bucket(self.bucket)
                logger.info(f"MinIO 存储桶已创建: {self.bucket}")
            else:
                logger.info(f"MinIO 存储桶已存在: {self.bucket}")
        except S3Error as e:
            logger.warning(f"MinIO 存储桶检查/创建失败: {e}")

    async def save(self, content: bytes, filename: str, metadata: Optional[dict] = None) -> str:
        from minio.error import S3Error

        start = time.time()
        content_type = self._guess_content_type(filename)
        try:
            data = io.BytesIO(content)
            self.client.put_object(
                self.bucket,
                filename,
                data=data,
                length=len(content),
                content_type=content_type,
                metadata=metadata or {},
            )

            elapsed = time.time() - start
            logger.info(
                f"MinIO 保存文件完成: {filename} ({len(content)} bytes, {elapsed:.3f}s)"
            )
            return f"{self.bucket}/{filename}"
        except S3Error as e:
            logger.error(f"MinIO 保存文件失败: {filename}, 错误: {e}")
            raise

    async def get_local_path(self, filename: str) -> str:
        """从 MinIO 下载文件到临时目录，返回本地路径"""
        from minio.error import S3Error

        start = time.time()
        try:
            # 创建临时目录
            tmp_dir = os.path.join(
                tempfile.gettempdir(), "kquery_storage", os.path.dirname(filename)
            )
            os.makedirs(tmp_dir, exist_ok=True)
            local_path = os.path.join(tmp_dir, os.path.basename(filename))

            # 下载文件
            self.client.fget_object(self.bucket, filename, local_path)
            elapsed = time.time() - start
            logger.info(
                f"MinIO 下载文件完成: {filename} -> {local_path} ({elapsed:.3f}s)"
            )
            return local_path
        except S3Error as e:
            logger.error(f"MinIO 下载文件失败: {filename}, 错误: {e}")
            raise FileNotFoundError(f"MinIO 文件不存在: {filename}")

    async def delete(self, filename: str) -> bool:
        from minio.error import S3Error

        try:
            self.client.remove_object(self.bucket, filename)
            logger.info(f"MinIO 文件已删除: {filename}")
            return True
        except S3Error as e:
            logger.warning(f"MinIO 删除文件失败: {filename}, 错误: {e}")
            return False

    async def exists(self, filename: str) -> bool:
        try:
            self.client.stat_object(self.bucket, filename)
            return True
        except Exception:
            return False

    @staticmethod
    def _guess_content_type(filename: str) -> str:
        """根据文件扩展名猜测 MIME 类型"""
        ext = os.path.splitext(filename)[1].lower()
        mapping = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".txt": "text/plain",
            ".md": "text/markdown",
            ".html": "text/html",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ".ppt": "application/vnd.ms-powerpoint",
            ".csv": "text/csv",
            ".xml": "application/xml",
            ".json": "application/json",
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
        }
        return mapping.get(ext, "application/octet-stream")


def get_storage_service() -> StorageService:
    """工厂函数：根据配置获取存储服务实例"""
    storage_settings = settings.storage

    if storage_settings.type == "minio":
        return MinioStorageService(
            endpoint=storage_settings.minio_endpoint,
            access_key=storage_settings.minio_access_key,
            secret_key=storage_settings.minio_secret_key,
            bucket=storage_settings.minio_bucket,
            secure=storage_settings.minio_secure,
            region=storage_settings.minio_region,
        )
    else:
        return LocalStorageService(upload_dir=storage_settings.upload_dir)
