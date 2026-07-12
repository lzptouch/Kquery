"""Gradio前端界面模块。

提供用户友好的Web界面，支持文件上传和智能问答。
"""

from app.webUI.gradio_app import create_gradio_app

__all__ = ["create_gradio_app"]