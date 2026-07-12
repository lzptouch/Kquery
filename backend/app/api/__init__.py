"""
KQuery API 模块

提供RESTful API接口，对外提供知识库查询服务
"""

from app.api.router import router
from app.api.main import app

__all__ = [
    "router",
    "app"
]