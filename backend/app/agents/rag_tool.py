"""RAG工具封装 - 将RAGService封装为LangChain Tool。

将现有的RAG功能封装为Agent可调用的工具，支持文档检索和查询。
"""

from typing import Type, Optional, Dict, Any
from pydantic import BaseModel, Field
from langchain_core.tools import BaseTool
from app.core.logger import get_logger
from app.services.rag_service import RAGService

logger = get_logger(__name__)


class RAGToolInput(BaseModel):
    """RAG工具输入参数"""
    query: str = Field(..., description="查询文本，用户的问题或搜索内容")
    collection_name: str = Field(..., description="向量集合名称，指定在哪个知识库中搜索")
    top_k: Optional[int] = Field(
        default=5, 
        description="返回结果数量，默认为5个最相关的结果"
    )
    enable_hyde: Optional[bool] = Field(
        default=True, 
        description="是否启用HyDE（假设性文档嵌入），可以提升检索效果"
    )


class RAGTool(BaseTool):
    """RAG检索工具 - 在知识库中搜索相关信息。

    用途：
    - 当用户询问关于特定主题、概念或事实的问题时使用
    - 需要从上传的文档或知识库中获取准确信息时使用
    - 用于验证事实、获取详细解释或查找相关示例

    使用场景：
    - "什么是机器学习？" - 从知识库中查找机器学习的定义和解释
    - "Python中如何实现多线程？" - 搜索相关编程示例
    - "帮我查找关于神经网络的应用案例" - 查找具体应用场景

    注意：当用户的问题需要从知识库中获取具体信息时使用此工具。
    """
    
    name: str = "rag_search"
    description: str = """
    在知识库中搜索相关信息。当用户询问关于特定主题、概念、事实或需要从上传的文档中获取信息时使用此工具。
    
    输入参数：
    - query: 用户的问题或搜索内容
    - collection_name: 知识库集合名称
    - top_k: 返回结果数量（可选，默认5）
    - enable_hyde: 是否启用智能检索增强（可选，默认开启）
    
    使用示例：
    - 输入: {"query": "什么是深度学习？", "collection_name": "tech_docs"}
    - 输入: {"query": "Python列表操作", "collection_name": "programming"}
    """
    args_schema: Type[BaseModel] = RAGToolInput
    
    def _run(self, query: str, collection_name: str, top_k: int = 5, enable_hyde: bool = True) -> str:
        """同步执行RAG检索"""
        import asyncio
        return asyncio.run(self._arun(query, collection_name, top_k, enable_hyde))
    
    async def _arun(self, query: str, collection_name: str, top_k: int = 5, enable_hyde: bool = True) -> str:
        """异步执行RAG检索"""
        try:
            logger.info(f"RAG工具调用: query='{query}', collection='{collection_name}', top_k={top_k}")
            
            # 创建RAGService实例
            rag_service = RAGService()
            
            # 调用RAG服务进行查询
            result = await rag_service.query_documents(
                query=query,
                collection_name=collection_name,
                top_k=top_k,
                enable_hyde=enable_hyde
            )
            
            if result.error_message:
                return f"检索失败: {result.error_message}"
            
            if not result.results:
                return f"未在知识库 '{collection_name}' 中找到与 '{query}' 相关的信息。"
            
            # 格式化检索结果
            formatted_results = []
            for i, retrieval_result in enumerate(result.results, 1):
                score = retrieval_result.score
                text = retrieval_result.text.strip()
                source = retrieval_result.metadata.get('file_name', '未知来源')
                chunk_type = retrieval_result.metadata.get('chunk_type', 'text')
                
                # 添加特殊标记指示内容类型
                type_label = ""
                if chunk_type == "image_description":
                    type_label = " [图像描述]"
                elif chunk_type == "table_description":
                    type_label = " [表格描述]"
                
                formatted_result = f"[{i}] 相关性: {score:.3f} | 来源: {source}{type_label}\n{text}"
                formatted_results.append(formatted_result)
            
            # 构建最终响应
            response = f"""在知识库 '{collection_name}' 中找到了 {len(result.results)} 个相关结果：

{"\n\n".join(formatted_results)}

基于这些信息，我可以为您提供详细的解释。请特别注意标记为 [图像描述] 或 [表格描述] 的结果，它们包含了文档中图像和表格的智能理解内容。"""
            
            logger.info(f"RAG检索完成，找到 {len(result.results)} 个结果")
            return response
            
        except Exception as e:
            logger.error(f"RAG工具执行失败: {e}", exc_info=True)
            return f"检索过程中出现错误: {str(e)}"


class DocumentProcessingToolInput(BaseModel):
    """文档处理工具输入参数"""
    file_path: str = Field(..., description="文件路径")
    collection_name: str = Field(..., description="向量集合名称")
    chunk_size: Optional[int] = Field(default=500, description="块大小")
    chunk_overlap: Optional[int] = Field(default=50, description="块重叠大小")


class DocumentProcessingTool(BaseTool):
    """文档处理工具 - 将文档添加到知识库。

    用途：
    - 当用户上传新文档时需要将其添加到知识库时使用
    - 用于扩展知识库的内容覆盖范围
    - 支持多种文件格式（PDF、Word、Excel、PPT、HTML、Markdown、Text）
    """
    
    name: str = "process_document"
    description: str = """
    处理文档并将其添加到知识库中。支持多种文件格式，包括PDF、Word、Excel、PPT、HTML、Markdown、Text。
    
    输入参数：
    - file_path: 要处理的文件路径
    - collection_name: 目标知识库集合名称
    - chunk_size: 文本块大小（可选，默认500）
    - chunk_overlap: 块重叠大小（可选，默认50）
    """
    args_schema: Type[BaseModel] = DocumentProcessingToolInput
    
    def _run(self, file_path: str, collection_name: str, chunk_size: int = 500, chunk_overlap: int = 50) -> str:
        """同步处理文档"""
        import asyncio
        return asyncio.run(self._arun(file_path, collection_name, chunk_size, chunk_overlap))
    
    async def _arun(self, file_path: str, collection_name: str, chunk_size: int = 500, chunk_overlap: int = 50) -> str:
        """异步处理文档"""
        try:
            logger.info(f"文档处理工具调用: file='{file_path}', collection='{collection_name}'")
            
            # 创建RAGService实例
            rag_service = RAGService()
            
            # 检查文件是否支持
            if not rag_service.is_file_supported(file_path):
                return f"不支持的文件类型: {file_path}"
            
            # 处理文档
            result = await rag_service.process_document(
                file_path=file_path,
                collection_name=collection_name,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap
            )
            
            if result.success:
                return f"文档处理成功！生成了 {result.chunk_count} 个文本块和 {result.embedding_count} 个向量，已添加到知识库 '{collection_name}'。"
            else:
                return f"文档处理失败: {result.error_message}"
                
        except Exception as e:
            logger.error(f"文档处理工具执行失败: {e}", exc_info=True)
            return f"文档处理过程中出现错误: {str(e)}"


# 工具注册表
TOOLS_REGISTRY = {
    "rag_search": RAGTool,
    "process_document": DocumentProcessingTool,
}


def get_rag_tools() -> Dict[str, BaseTool]:
    """获取所有RAG相关工具"""
    return {
        "rag_search": RAGTool(),
        "process_document": DocumentProcessingTool(),
    }