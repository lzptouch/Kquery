"""笔记数据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class NoteCreateRequest(BaseModel):
    """创建笔记请求。"""
    title: str = Field(default="未命名笔记", description="笔记标题")
    content: str = Field(default="", description="Markdown 内容")
    collection_name: str = Field(default="default", description="关联知识库")
    tags: List[str] = Field(default_factory=list, description="标签列表")


class NoteUpdateRequest(BaseModel):
    """更新笔记请求。"""
    title: Optional[str] = Field(default=None, description="笔记标题")
    content: Optional[str] = Field(default=None, description="Markdown 内容")
    collection_name: Optional[str] = Field(default=None, description="关联知识库")
    tags: Optional[List[str]] = Field(default=None, description="标签列表")


class NoteResponse(BaseModel):
    """笔记响应。"""
    id: str
    title: str
    content: str
    collection_name: str
    tags: List[str] = []
    source_type: str = "user_note"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_storage(cls, note_id: str, meta: dict, content: str) -> "NoteResponse":
        """从存储数据构建响应。"""
        return cls(
            id=note_id,
            title=meta.get("title", "未命名笔记"),
            content=content,
            collection_name=meta.get("collection_name", "default"),
            tags=meta.get("tags", []),
            source_type=meta.get("source_type", "user_note"),
            created_at=meta.get("created_at", ""),
            updated_at=meta.get("updated_at", ""),
        )


class AIWritingRequest(BaseModel):
    """AI 写作请求。"""
    text: str = Field(description="文本内容")
    style: str = Field(default="专业正式", description="风格/tone")
    target_lang: str = Field(default="英文", description="目标语言（翻译时使用）")


class NoteListResponse(BaseModel):
    """笔记列表响应。"""
    notes: List[NoteResponse]
    total: int
