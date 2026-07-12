"""聊天/Agent 相关数据模型。"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field, field_validator
import base64
import re


class ChatImage(BaseModel):
    """聊天消息中的图片"""
    id: Optional[str] = Field(None, description="图片ID")
    data_url: str = Field(..., description="Base64 DataURL")
    type: Optional[str] = Field(default="image/jpeg", description="图片MIME类型")
    filename: Optional[str] = Field(None, description="图片文件名")

    @field_validator("data_url")
    @classmethod
    def validate_data_url(cls, v: str) -> str:
        if not v.startswith("data:image/"):
            raise ValueError("图片必须是 data:image/...;base64, 格式")
        return v

    def mime_type(self) -> str:
        """从 DataURL 解析 MIME 类型"""
        match = re.match(r"^data:image/([^;]+);base64,", self.data_url)
        if match:
            return f"image/{match.group(1)}"
        return self.type or "image/jpeg"

    def extract_base64(self) -> bytes:
        """从 DataURL 提取 Base64 字节"""
        match = re.match(r"^data:image/[^;]+;base64,(.*)$", self.data_url, re.DOTALL)
        if not match:
            raise ValueError("Invalid image data URL")
        return base64.b64decode(match.group(1))


class ChatRequest(BaseModel):
    """聊天请求"""
    query: str = Field(..., description="用户问题")
    session_id: Optional[str] = Field(None, description="会话ID")
    collection_name: str = Field(default="default", description="知识库集合名称")
    enable_rag: bool = Field(default=True, description="是否启用RAG检索")
    images: Optional[List[ChatImage]] = Field(None, description="图片列表")

    @property
    def has_images(self) -> bool:
        return bool(self.images and len(self.images) > 0)


