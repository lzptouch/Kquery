"""通用数据模型。"""

from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class StandardResponse(BaseModel):
    """标准API响应格式"""
    success: bool
    data: Optional[Any] = None
    message: Optional[str] = None
    error: Optional[str] = None
