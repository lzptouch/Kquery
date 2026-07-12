"""
文档处理相关数据模式
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class DocumentProcessRequest(BaseModel):
    """文档处理请求"""
    file_path: str = Field(..., description="文件路径")
    collection_name: str = Field(..., description="向量集合名称")
    chunk_size: Optional[int] = Field(500, description="块大小（字符数）")
    chunk_overlap: Optional[int] = Field(50, description="块重叠大小（字符数）")
    metadata: Optional[Dict[str, Any]] = Field(None, description="额外的元数据")


class DocumentBatchRequest(BaseModel):
    """批量文档处理请求"""
    file_paths: List[str] = Field(..., description="文件路径列表")
    collection_name: str = Field(..., description="向量集合名称")
    chunk_size: Optional[int] = Field(500, description="块大小（字符数）")
    chunk_overlap: Optional[int] = Field(50, description="块重叠大小（字符数）")
    metadata: Optional[Dict[str, Any]] = Field(None, description="额外的元数据")
    max_concurrency: Optional[int] = Field(3, description="最大并发数")


class DocumentProcessResult(BaseModel):
    """文档处理结果"""
    success: bool = Field(..., description="是否成功")
    file_path: str = Field(..., description="文件路径")
    chunk_count: int = Field(..., description="生成的块数量")
    embedding_count: int = Field(..., description="生成的向量数量")
    collection_name: str = Field(..., description="集合名称")
    error_message: Optional[str] = Field(None, description="错误消息（如果有）")


class TextAddRequest(BaseModel):
    """添加文本请求"""
    text: str = Field(..., description="文本内容")
    collection_name: str = Field(..., description="向量集合名称")
    metadata: Optional[Dict[str, Any]] = Field(None, description="元数据")


class TextAddResult(BaseModel):
    """添加文本结果"""
    success: bool = Field(..., description="是否成功")
    text_length: int = Field(..., description="文本长度")
    collection_name: str = Field(..., description="集合名称")
    error_message: Optional[str] = Field(None, description="错误消息（如果有）")