"""Kquery 知识库查询项目。

提供完整的知识库检索增强生成（RAG）服务，支持多格式文档处理和语义检索。

主要功能模块：
- app.api: Web API接口
- app.services: 业务逻辑层（文件处理、嵌入、检索）
- app.repositories: 数据访问层（Repository模式）
- app.models: Pydantic数据模型定义
- app.utils: 工具函数和配置
- app.agents: Agent模块和工具系统

使用示例：
    from app.services.rag_service import RAGService

    # 初始化服务
    service = RAGService()

    # 处理文档
    result = await service.process_document(
        file_path="document.pdf",
        collection_name="my_collection"
    )

    # 查询文档
    results = await service.query_documents(
        query="什么是机器学习？",
        collection_name="my_collection",
        top_k=5
    )
"""

from __future__ import annotations

__version__ = "1.0.0"
