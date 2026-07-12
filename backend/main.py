#!/usr/bin/env python3
"""
KQuery Pro 多模态知识库查询服务 - 主入口文件

提供完整的多模态知识库查询服务，支持文档、音频、视频等多种格式数据处理
集成阿里云和科大讯飞AI模型技术，实现跨模态语义检索
新增Gradio前端界面和智能问答Agent功能
"""

import uvicorn
import threading
import time
from app.core.settings import get_settings
from app.core.logger import get_logger

config = get_settings()

logger = get_logger(__name__)

def start_fastapi():
    """启动FastAPI服务"""
    logger.info("启动 FastAPI 服务...")
    logger.info(f"API地址: http://{config.api_host}:{config.api_port}")
    logger.info(f"API文档: http://{config.api_host}:{config.api_port}/docs")
    
    uvicorn.run(
        "app.api.main:app",
        host=config.api_host,
        port=config.api_port,
        reload=config.debug,
        log_level="info",
        access_log=True
    )

def start_gradio():
    """启动Gradio前端界面"""
    # 等待FastAPI服务启动
    time.sleep(2)
    
    logger.info("启动 Gradio 前端界面...")
    
    try:
        from app.webUI import create_gradio_app
        
        gradio_app = create_gradio_app()
        
        # Gradio默认端口为7860
        gradio_port = 7860
        
        logger.info(f"Gradio界面地址: http://{config.api_host}:{gradio_port}")
        logger.info("Gradio界面功能:")
        logger.info("- 文件上传: 支持PDF、Word、Excel、PPT、HTML、Markdown、Text等格式")
        logger.info("- 智能问答: 基于上传文档的自然语言对话")
        logger.info("- 会话管理: 多轮对话记忆和上下文理解")
        
        gradio_app.launch(
            server_name=config.api_host,
            server_port=gradio_port,
            share=False,
            quiet=False
        )
        
    except ImportError as e:
        logger.error(f"Gradio模块导入失败: {e}")
        logger.info("请确保已安装gradio依赖: pip install gradio")
    except Exception as e:
        logger.error(f"Gradio启动失败: {e}")

def main():
    """主函数 - 启动FastAPI和Gradio服务"""
    
    logger.info("启动 KQuery Pro 多模态知识库查询服务...")
    logger.info(f"环境: {config.environment}")
    logger.info(f"调试模式: {config.debug}")
    logger.info("服务包含:")
    logger.info("- FastAPI后端: RESTful API接口")
    logger.info("- Gradio前端: 用户友好界面")
    logger.info("- 智能Agent: 问答和文档处理")
    
    if config.debug:
        # 调试模式下，Gradio在守护线程启动，FastAPI在主线程启动
        # 注意：uvicorn reload 依赖 signal，不能在子线程中使用
        gradio_thread = threading.Thread(target=start_gradio, daemon=True)
        gradio_thread.start()
    
    # 生产/调试模式均在主线程启动 FastAPI
    start_fastapi()

if __name__ == "__main__":
    main()