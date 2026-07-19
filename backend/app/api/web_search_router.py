"""联网搜索 API 路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.logger import get_logger
from app.services.web_search import WebSearchService, get_web_search_service
from app.services.web_fetcher import QueryClassifier

logger = get_logger(__name__)

router = APIRouter(prefix="/web-search", tags=["联网搜索"])


class WebSearchRequest(BaseModel):
    """联网搜索请求。"""
    query: str = Field(description="搜索查询")
    top_n: int = Field(default=5, ge=1, le=20, description="返回结果数")
    fetch_content: bool = Field(default=False, description="是否抓取网页正文")


class ClassifyRequest(BaseModel):
    """查询分类请求。"""
    query: str = Field(description="待分类查询")


@router.post("")
async def web_search(request: WebSearchRequest):
    """执行联网搜索。

    Args:
        request: 搜索请求
    """
    service = get_web_search_service()
    results = await service.search(
        query=request.query,
        top_n=request.top_n,
        fetch_content=request.fetch_content,
    )

    return {
        "success": True,
        "query": request.query,
        "total": len(results),
        "results": [
            {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                "content": r.content,
                "source": r.source,
                "published_at": r.published_at,
            }
            for r in results
        ],
    }


@router.post("/classify")
async def classify_query(request: ClassifyRequest):
    """判断查询是否需要联网搜索。

    Args:
        request: 分类请求
    """
    needs_web = QueryClassifier.needs_web_search(request.query)
    return {
        "query": request.query,
        "needs_web_search": needs_web,
        "method": "rule",
    }


@router.get("/test")
async def test_search_provider():
    """测试搜索引擎连通性。"""
    service = get_web_search_service()
    try:
        results = await service.search("测试", top_n=1)
        return {
            "success": True,
            "message": "搜索引擎可用",
            "test_query": "测试",
            "result_count": len(results),
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"搜索引擎不可用: {str(e)}",
        }
