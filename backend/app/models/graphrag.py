"""GraphRAG 相关数据模型。"""

from typing import Any, Dict, List, Optional
from datetime import datetime
from pydantic import BaseModel, Field


class HybridSearchRequest(BaseModel):
    """混合检索请求。"""
    query: str = Field(..., min_length=1, max_length=2000, description="用户查询")
    collection_name: str = Field(..., description="Milvus集合名称")
    kb_id: Optional[int] = Field(None, description="知识库ID（用于图谱查询）")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")
    enable_hyde: bool = Field(True, description="是否启用HyDE假设性文档嵌入")
    enable_entity_extraction: bool = Field(True, description="是否启用实体抽取")
    enable_graph_expansion: bool = Field(True, description="是否启用图谱扩展")


class AskRequest(BaseModel):
    """智能问答请求。"""
    query: str = Field(..., min_length=1, max_length=2000, description="用户问题")
    collection_name: str = Field(..., description="Milvus集合名称")
    kb_id: Optional[int] = Field(None, description="知识库ID")
    system_prompt: Optional[str] = Field(None, description="自定义系统提示词")
    conversation_history: List[Dict[str, str]] = Field(
        default_factory=list,
        description="对话历史 [{\"role\": \"user|assistant\", \"content\": \"...\"}]"
    )


class EntityExtractionRequest(BaseModel):
    """实体抽取请求。"""
    query: str = Field(..., min_length=1, description="查询文本")
    kb_id: Optional[int] = Field(None, description="知识库ID（用于匹配数据库实体）")


class StandardResponse(BaseModel):
    """标准API响应格式。"""
    success: bool
    message: str
    data: Optional[Any] = None
    error: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
