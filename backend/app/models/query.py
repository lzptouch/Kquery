"""
查询相关数据模式
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class RetrievalResult(BaseModel):
    """检索结果"""
    text: str = Field(..., description="检索到的文本")
    score: float = Field(..., description="相似度分数")
    metadata: Dict[str, Any] = Field(..., description="元数据")
    source: Optional[str] = Field(None, description="来源文件路径")


class QueryRequest(BaseModel):
    """查询请求"""
    query: str = Field(..., description="查询文本")
    collection_name: str = Field(..., description="向量集合名称")
    top_k: Optional[int] = Field(5, description="返回结果数量")
    enable_hyde: Optional[bool] = Field(False, description="是否启用HyDE")
    filters: Optional[Dict[str, Any]] = Field(None, description="过滤条件")


class QueryResult(BaseModel):
    """查询结果"""
    query: str = Field(..., description="查询文本")
    results: List[RetrievalResult] = Field(..., description="检索结果列表")
    total_count: int = Field(..., description="总结果数")
    collection_name: str = Field(..., description="集合名称")
    error_message: Optional[str] = Field(None, description="错误消息（如果有）")


class CollectionStats(BaseModel):
    """集合统计信息"""
    collection_name: str = Field(..., description="集合名称")
    document_count: int = Field(..., description="文档数量")
    vector_count: int = Field(..., description="向量数量")
    created_at: Optional[str] = Field(None, description="创建时间")
    last_updated: Optional[str] = Field(None, description="最后更新时间")


class CollectionCreateRequest(BaseModel):
    """创建集合请求"""
    collection_name: str = Field(..., description="集合名称")
    dimension: Optional[int] = Field(4096, description="向量维度")
    description: Optional[str] = Field(None, description="集合描述")


class CollectionDeleteRequest(BaseModel):
    """删除集合请求"""
    collection_name: str = Field(..., description="集合名称")
    confirm: bool = Field(False, description="确认删除")