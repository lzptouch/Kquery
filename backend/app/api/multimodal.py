"""多模态内容检索 API (v2.0)。

查询 Milvus 中存储的多模态文档块：
- image_description: 图像 AI 描述
- table: 结构化表格数据
- table_description: 表格洞察描述
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import time
import uuid

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.knowledge_graph import StandardResponse
from app.core.logger import get_logger
from app.core.audit_logger import audit_logger


logger = get_logger(__name__)
router = APIRouter(tags=["multimodal"])


# ============================================================
# 请求/响应模型
# ============================================================

class MultimodalSearchRequest(BaseModel):
    """多模态内容检索请求。"""
    query: str = Field(..., min_length=1, max_length=500, description="检索关键词")
    collection_name: str = Field(default="default", description="Milvus 集合名")
    chunk_types: List[str] = Field(
        default=["image_description", "table", "table_description"],
        description="要检索的 chunk_type 列表"
    )
    top_k: int = Field(default=10, ge=1, le=50)
    filter: Optional[str] = Field(default=None, description="额外的 metadata 过滤表达式")


class MultimodalItem(BaseModel):
    """单个多模态内容项。"""
    id: str
    chunk_type: str
    content: str
    score: float
    metadata: Dict[str, Any]
    source_file: str = ""
    page_number: Optional[int] = None


# ============================================================
# 辅助函数
# ============================================================

def _mock_multimodal_results(chunk_types: List[str], top_k: int) -> List[Dict[str, Any]]:
    """模拟多模态检索结果（实际环境应调用 Milvus 向量检索）。"""
    results = []
    
    # 图像描述模拟数据
    if "image_description" in chunk_types:
        image_items = [
            {
                "id": "img_001",
                "chunk_type": "image_description",
                "content": "系统架构图：包含前端 React 应用、后端 FastAPI 服务、Milvus 向量数据库和 Neo4j 知识图谱四个主要组件，通过 RESTful API 和 WebSocket 进行通信。",
                "score": 0.92,
                "metadata": {"file_name": "architecture.png", "page_number": 1, "image_width": 1200, "image_height": 800},
                "source_file": "docs/architecture.png",
                "page_number": 1,
            },
            {
                "id": "img_002",
                "chunk_type": "image_description",
                "content": "数据流图：文档从上传开始，经过解析、分块、嵌入、存储到 Milvus，同时提取实体关系写入 Neo4j。",
                "score": 0.88,
                "metadata": {"file_name": "data_flow.png", "page_number": 2},
                "source_file": "docs/data_flow.png",
                "page_number": 2,
            },
            {
                "id": "img_003",
                "chunk_type": "image_description",
                "content": "GraphRAG 流程图：用户查询经过实体抽取、图谱扩展、向量检索、结果融合、答案生成五个阶段。",
                "score": 0.85,
                "metadata": {"file_name": "graphrag_flow.png", "page_number": 3},
                "source_file": "docs/graphrag_flow.png",
                "page_number": 3,
            },
        ]
        results.extend(image_items[:top_k])
    
    # 表格模拟数据
    if "table" in chunk_types:
        table_items = [
            {
                "id": "tbl_001",
                "chunk_type": "table",
                "content": "| 组件 | 技术栈 | 版本 |\n|------|--------|------|\n| 前端 | React + TypeScript | 18.2 |\n| 后端 | FastAPI | 0.111 |\n| 向量库 | Milvus | 2.4 |\n| 图数据库 | Neo4j | 5.0 |",
                "score": 0.95,
                "metadata": {"file_name": "tech_stack.xlsx", "page_number": 1, "row_count": 5, "col_count": 3},
                "source_file": "docs/tech_stack.xlsx",
                "page_number": 1,
            },
            {
                "id": "tbl_002",
                "chunk_type": "table",
                "content": "| 查询类型 | 平均延迟(ms) | 准确率(%) |\n|----------|-------------|----------|\n| 语义检索 | 120 | 85.5 |\n| 图谱检索 | 200 | 92.3 |\n| 混合检索 | 180 | 94.1 |",
                "score": 0.90,
                "metadata": {"file_name": "performance_report.xlsx", "page_number": 2, "row_count": 4, "col_count": 3},
                "source_file": "docs/performance_report.xlsx",
                "page_number": 2,
            },
        ]
        results.extend(table_items[:top_k])
    
    # 表格描述模拟数据
    if "table_description" in chunk_types:
        desc_items = [
            {
                "id": "desc_001",
                "chunk_type": "table_description",
                "content": "技术栈表格展示了系统各层采用的技术选型。前端使用 React 18.2 + TypeScript，提供类型安全和高性能组件渲染；后端基于 FastAPI 0.111，支持异步处理和自动 OpenAPI 文档生成；向量存储使用 Milvus 2.4 实现高效的语义检索；图数据库 Neo4j 5.0 用于存储实体关系网络。",
                "score": 0.93,
                "metadata": {"file_name": "tech_stack.xlsx", "table_id": "tbl_001", "summary_type": "insight"},
                "source_file": "docs/tech_stack.xlsx",
                "page_number": 1,
            },
            {
                "id": "desc_002",
                "chunk_type": "table_description",
                "content": "性能对比表格揭示了不同检索模式的延迟与准确率权衡。语义检索延迟最低（120ms）但准确率相对偏低（85.5%）；图谱检索准确率较高（92.3%）但延迟增加（200ms）；混合检索在两者间取得平衡，以 180ms 延迟实现 94.1% 的准确率，是生产环境推荐方案。",
                "score": 0.91,
                "metadata": {"file_name": "performance_report.xlsx", "table_id": "tbl_002", "summary_type": "insight"},
                "source_file": "docs/performance_report.xlsx",
                "page_number": 2,
            },
        ]
        results.extend(desc_items[:top_k])
    
    # 按分数排序并截断
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


# ============================================================
# 端点定义
# ============================================================

@router.get("/multimodal/types", response_model=StandardResponse)
async def list_multimodal_types():
    """列出支持的多模态内容类型。"""
    return StandardResponse(
        success=True,
        data={
            "types": [
                {
                    "id": "image_description",
                    "name": "图像描述",
                    "description": "AI 自动生成的图像内容描述（图表、架构图、流程图等）",
                    "icon": "image",
                },
                {
                    "id": "table",
                    "name": "结构化表格",
                    "description": "文档中提取的表格数据（CSV、Excel、Markdown 表格等）",
                    "icon": "table",
                },
                {
                    "id": "table_description",
                    "name": "表格洞察",
                    "description": "AI 对表格数据的深度解读和分析摘要",
                    "icon": "lightbulb",
                },
            ]
        }
    )


@router.post("/multimodal/search", response_model=StandardResponse)
async def search_multimodal_content(
    request: Request,
    request_body: MultimodalSearchRequest,
):
    """检索多模态内容。"""
    start_time = time.perf_counter()
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    try:
        # TODO: 实际生产环境中，应调用 RAGService 或 Milvus 客户端进行向量检索
        # 按 chunk_type 过滤，使用 metadata["chunk_type"] 条件
        results = _mock_multimodal_results(
            chunk_types=request_body.chunk_types,
            top_k=request_body.top_k
        )
        
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/multimodal/search",
            query_text=request_body.query,
            collection_name=request_body.collection_name,
            response_count=len(results),
            duration_ms=duration_ms,
            status="success",
            request_id=request_id,
        )
        
        return StandardResponse(
            success=True,
            data={
                "query": request_body.query,
                "chunk_types": request_body.chunk_types,
                "total": len(results),
                "results": results,
            },
            message=f"检索到 {len(results)} 条多模态内容"
        )
    
    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/multimodal/search",
            query_text=request_body.query if request_body else None,
            collection_name=request_body.collection_name if request_body else None,
            duration_ms=duration_ms,
            status="failure",
            request_id=request_id,
        )
        logger.error(f"多模态检索失败: {e}", exc_info=True)
        return StandardResponse(
            success=False,
            data=None,
            message=f"检索失败: {str(e)}"
        )


@router.get("/multimodal/content", response_model=StandardResponse)
async def list_multimodal_content(
    collection_name: str = Query(default="default", description="Milvus 集合名"),
    chunk_type: Optional[str] = Query(None, description="按 chunk_type 过滤"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """列出多模态内容（支持分页）。"""
    types_filter = [chunk_type] if chunk_type else ["image_description", "table", "table_description"]
    results = _mock_multimodal_results(chunk_types=types_filter, top_k=limit + offset)
    paginated = results[offset:offset + limit]
    
    return StandardResponse(
        success=True,
        data={
            "collection": collection_name,
            "chunk_type": chunk_type,
            "total": len(results),
            "limit": limit,
            "offset": offset,
            "results": paginated,
        }
    )
