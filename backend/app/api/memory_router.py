"""长期记忆 API 路由。"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.logger import get_logger
from app.services.long_term_memory_service import LongTermMemoryService

logger = get_logger(__name__)

router = APIRouter(prefix="/memory", tags=["长期记忆"])


class MemoryFactResponse(BaseModel):
    """记忆事实响应。"""
    id: str
    content: str
    fact_type: str
    confidence: float
    created_at: str


class MemoryListResponse(BaseModel):
    """记忆列表响应。"""
    facts: List[MemoryFactResponse]
    total: int


class ExtractMemoryRequest(BaseModel):
    """手动提取记忆请求。"""
    text: str = Field(description="待提取的文本")
    user_id: str = Field(default="default", description="用户 ID")


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    user_id: str = Query(default="default", description="用户 ID"),
    fact_type: Optional[str] = Query(default=None, description="按类型过滤"),
):
    """获取用户长期记忆列表。"""
    service = LongTermMemoryService()
    # 检索全部记忆
    facts = await service.retrieve_facts(user_id, "全部记忆", top_k=50)

    if fact_type:
        facts = [f for f in facts if f.fact_type == fact_type]

    return MemoryListResponse(
        facts=[
            MemoryFactResponse(
                id=f.id,
                content=f.content,
                fact_type=f.fact_type,
                confidence=f.confidence,
                created_at=f.created_at,
            )
            for f in facts
        ],
        total=len(facts),
    )


@router.delete("/{fact_id}")
async def delete_memory(
    fact_id: str,
    user_id: str = Query(default="default", description="用户 ID"),
):
    """删除一条记忆。"""
    service = LongTermMemoryService()
    success = await service.delete_fact(user_id, fact_id)
    if not success:
        raise HTTPException(status_code=404, detail="记忆不存在或删除失败")
    return {"success": True, "message": "记忆已删除"}


@router.delete("")
async def clear_all_memories(
    user_id: str = Query(default="default", description="用户 ID"),
):
    """清空用户所有记忆。"""
    service = LongTermMemoryService()
    success = await service.delete_all_facts(user_id)
    return {"success": success, "message": "所有记忆已清空"}


@router.post("/extract")
async def extract_memory_from_text(request: ExtractMemoryRequest):
    """手动从文本提取记忆（调试用）。"""
    service = LongTermMemoryService()
    messages = [{"role": "user", "content": request.text}]
    facts = await service.extract_facts(messages, user_id=request.user_id)

    return {
        "success": True,
        "facts": [
            {"content": f.content, "type": f.fact_type}
            for f in facts
        ],
        "total": len(facts),
    }
