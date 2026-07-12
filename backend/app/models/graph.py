"""知识图谱相关数据模型。"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class BuildGraphRequest(BaseModel):
    """从文本构建图谱的请求"""
    text: str = Field(..., min_length=10, max_length=50000, description="待分析的文本内容")
    doc_id: Optional[str] = Field(None, description="来源文档ID")
    kb_id: Optional[int] = Field(None, description="知识库ID")
    context: Optional[str] = Field(None, max_length=1000, description="额外上下文（如标题）")


class BuildGraphFromCollectionRequest(BaseModel):
    """从集合构建知识图谱的请求"""
    collection_name: str = Field(..., min_length=1, description="Milvus集合名称")
    doc_limit: int = Field(default=50, ge=1, le=200, description="读取的最大文档块数量")
    kb_id: Optional[int] = Field(None, description="知识库ID")
    document_ids: Optional[List[str]] = Field(default=None, description="仅对指定文档对应的 chunk 构建 KG")
    use_chunk_build: bool = Field(default=True, description="是否按 chunk 批量抽取")


class SearchEntitiesRequest(BaseModel):
    """搜索实体的请求"""
    keyword: str = Field(..., min_length=1, max_length=200)
    entity_type: Optional[str] = None
    kb_id: Optional[int] = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class SubgraphRequest(BaseModel):
    """子图提取请求"""
    center_ids: List[str] = Field(..., min_length=1, max_length=20, description="中心实体ID列表")
    radius: int = Field(default=2, ge=1, le=3, description="半径(跳数)")
    max_nodes: int = Field(default=200, ge=10, le=500)


class VisualizationRequest(BaseModel):
    """可视化数据请求"""
    center_ids: List[str] = Field(..., min_length=1, max_length=20)
    radius: int = Field(default=2, ge=1, le=3)
    max_nodes: int = Field(default=200, ge=10, le=500)
    format: str = Field(default="d3_force", description="d3_force|cytoscape|echarts_graph|vis_network|raw_json")
    layout: str = Field(default="force", description="force|hierarchical|circular|radial|grid|concentric")
    include_properties: bool = Field(default=False)
    highlight_paths: Optional[List[List[str]]] = Field(default=None)


class PathQueryRequest(BaseModel):
    """路径查询请求"""
    source_id: str = Field(..., description="起始实体ID")
    target_id: str = Field(..., description="目标实体ID")
    max_depth: int = Field(default=5, ge=1, le=6)
    find_all: bool = Field(default=False, description="是否查找所有路径")
