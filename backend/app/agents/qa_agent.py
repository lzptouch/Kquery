"""问答Agent系统 - 基于LangGraph构建的智能问答Agent。

集成RAG工具和对话记忆，提供智能问答功能。
使用 langgraph.prebuilt.create_react_agent 替代已废弃的 langchain.agents。
"""

import asyncio
from typing import Dict, Any, Optional, List
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from app.core.settings import get_settings
from app.core.logger import get_logger

settings = get_settings()
from app.agents.rag_tool import get_rag_tools
from app.agents.memory import conversation_memory

logger = get_logger(__name__)


class QAAgent:
    """问答Agent - 基于LangGraph ReAct模式构建的智能问答系统。

    特点：
    - 集成RAG工具进行知识检索
    - 支持多轮对话记忆
    - 使用ReAct模式进行推理和工具调用
    - 支持异步处理
    """

    def __init__(self,
                 model_name: str = None,
                 temperature: float = 0.1,
                 max_iterations: int = 5,
                 enable_rag: bool = True):
        """初始化问答Agent

        Args:
            model_name: 使用的LLM模型名称，默认从配置读取
            temperature: 模型温度参数
            max_iterations: 最大推理迭代次数
            enable_rag: 是否启用RAG功能
        """
        self.model_name = model_name or settings.llm.agent_model_name
        self.temperature = temperature
        self.max_iterations = max_iterations
        self.enable_rag = enable_rag

        # 初始化LLM（使用独立的Agent LLM配置）
        from app.core.llm_client import is_llm_configured
        self._llm_configured = is_llm_configured("agent")
        self.llm = ChatOpenAI(
            model=self.model_name,
            temperature=temperature,
            openai_api_base=settings.llm.agent_api_base,
            openai_api_key=settings.llm.agent_api_key or "sk-placeholder",
            request_timeout=120
        )

        # 初始化工具
        self.tools = self._initialize_tools()

        # 初始化Agent图
        self.agent_graph = self._create_agent_graph()

        logger.info(f"问答Agent初始化完成，模型: {self.model_name}, 启用RAG: {enable_rag}")

    def _initialize_tools(self) -> List[BaseTool]:
        """初始化可用工具"""
        tools = []

        if self.enable_rag:
            # 添加RAG相关工具
            rag_tools = get_rag_tools()
            tools.extend(rag_tools.values())

        logger.info(f"初始化了 {len(tools)} 个工具")
        return tools

    def _create_system_prompt(self) -> str:
        """创建系统提示词"""
        return """你是KQuery智能助手，一个专业的文档知识问答专家。

你的能力：
1. 使用RAG工具在知识库中搜索相关信息
2. 基于检索到的信息提供准确、详细的回答
3. 保持对话的连贯性和上下文理解
4. 当信息不足时，会明确告知用户
5. 特别注意理解和整合文档中的图像和表格内容

重要说明 - 图像和表格处理：
- 知识库中可能包含标记为 [图像描述] 的内容，这是对文档中图像的AI智能理解
- 知识库中可能包含标记为 [表格描述] 的内容，这是对文档中表格的AI智能理解
- 请优先利用这些图像和表格描述来回答用户的问题
- 当用户询问关于图表、插图、示意图或表格的问题时，特别关注这些标记的内容

工具使用指南：
- 当用户询问关于特定主题、概念或事实的问题时，使用rag_search工具
- 当需要处理用户上传的文档时，使用process_document工具
- 优先使用知识库中的信息，确保回答的准确性
- 如果知识库中没有相关信息，请诚实地告知用户

对话风格：
- 专业、友好、乐于助人
- 回答要详细、有条理
- 引用知识库中的信息时要注明来源
- 保持对话的自然流畅

重要提示：
- 始终基于事实和知识库信息回答问题
- 不要编造或猜测信息
- 如果无法确定答案，请明确说明
- 保持对话的上下文连贯性
"""

    def _create_agent_graph(self):
        """使用 langgraph 创建 Agent 图"""
        if not self.tools:
            raise ValueError("Agent需要至少一个工具才能正常工作")

        system_prompt = self._create_system_prompt()

        graph = create_react_agent(
            model=self.llm,
            tools=self.tools,
            prompt=system_prompt,
        )

        return graph

    def _create_general_system_prompt(self) -> str:
        """创建通用助手系统提示词（无知识库时使用）"""
        return """你是KQuery智能助手，一个通用型AI助手。

你的能力：
1. 回答用户的各类问题，包括知识问答、创意写作、代码解释、逻辑推理等
2. 保持对话的连贯性和上下文理解
3. 当问题超出你的知识范围时，会明确告知用户
4. 提供详细、有条理、易于理解的回答

对话风格：
- 专业、友好、乐于助人
- 回答要详细、有条理
- 保持对话的自然流畅
- 根据用户的问题类型调整回答风格

重要提示：
- 始终基于事实和已有知识回答问题
- 不要编造或猜测信息
- 如果无法确定答案，请明确说明
- 保持对话的上下文连贯性
- 当前没有关联知识库，你的回答基于训练数据
"""

    async def chat_general(self,
                          user_input: str,
                          session_id: str,
                          **kwargs) -> Dict[str, Any]:
        """通用助手对话（无知识库时使用）

        Args:
            user_input: 用户输入
            session_id: 会话ID
            **kwargs: 其他参数

        Returns:
            包含响应和元数据的字典
        """
        try:
            logger.info(f"通用助手对话请求 - 会话: {session_id}, 输入: {user_input}")

            # 确保会话存在
            await conversation_memory.create_session(session_id)

            # 添加用户消息到记忆
            await conversation_memory.add_message(session_id, "user", user_input)

            # 获取对话上下文
            conversation_context = await conversation_memory.get_conversation_context(session_id)

            # 构建消息列表
            messages = [SystemMessage(content=self._create_general_system_prompt())]
            if conversation_context:
                messages.append(SystemMessage(
                    content=f"当前对话上下文：\n{conversation_context}"
                ))
            messages.append(HumanMessage(content=user_input))

            # 如果 LLM 未配置，使用 Mock 数据
            if not self._llm_configured:
                logger.warning("Agent API 未配置，使用 Mock 数据")
                from app.core.mock_data import get_mock_data
                mock_result = get_mock_data("chat_response", user_input=user_input, session_id=session_id)
                response_text = mock_result.get("response", "抱歉，LLM 未配置，当前使用 Mock 数据模式。")
            else:
                # 直接调用 LLM
                response = await self.llm.ainvoke(messages)
                response_text = response.content if hasattr(response, 'content') else str(response)

            if not response_text:
                response_text = "抱歉，我暂时无法回答这个问题。"

            # 添加助手消息到记忆
            await conversation_memory.add_message(session_id, "assistant", response_text, {
                "mode": "general"
            })

            # 构建返回结果
            session_stats = conversation_memory.get_session_stats(session_id)

            return {
                "success": True,
                "response": response_text,
                "session_id": session_id,
                "conversation_length": session_stats.get("total_messages", 0),
                "timestamp": session_stats.get("last_message_time"),
                "mode": "general"
            }

        except Exception as e:
            logger.error(f"通用助手对话失败: {e}", exc_info=True)

            error_response = "抱歉，我在处理您的请求时遇到了问题。请稍后重试。"
            await conversation_memory.add_message(session_id, "assistant", error_response, {
                "error": str(e)
            })

            return {
                "success": False,
                "response": error_response,
                "error": str(e),
                "session_id": session_id
            }

    async def chat(self,
                  user_input: str,
                  session_id: str,
                  collection_name: str = "default",
                  enable_rag: bool = True,
                  image_paths: Optional[List[str]] = None,
                  **kwargs) -> Dict[str, Any]:
        """与Agent进行对话

        Args:
            user_input: 用户输入
            session_id: 会话ID
            collection_name: 默认知识库集合名称
            enable_rag: 是否启用RAG
            image_paths: 图片路径列表（含图片时优先走多模态）
            **kwargs: 其他参数

        Returns:
            包含响应和元数据的字典
        """
        # 含图片时优先走多模态（无论 RAG 是否启用）
        if image_paths and len(image_paths) > 0:
            return await self._chat_multimodal(user_input, session_id, image_paths)
        if not enable_rag or not collection_name:
            return await self.chat_general(user_input, session_id, **kwargs)
        return await self._chat_rag(user_input, session_id, collection_name, **kwargs)

    async def _chat_rag(self,
                        user_input: str,
                        session_id: str,
                        collection_name: str = "default",
                        **kwargs) -> Dict[str, Any]:
        """RAG 知识库问答处理"""
        try:
            logger.info(f"Agent对话请求 - 会话: {session_id}, 输入: {user_input}")

            # 确保会话存在
            await conversation_memory.create_session(session_id)

            # 添加用户消息到记忆
            await conversation_memory.add_message(session_id, "user", user_input)

            # 获取对话上下文
            conversation_context = await conversation_memory.get_conversation_context(session_id)

            # 直接调用 GraphRAG 引擎生成答案（获取溯源信息）
            from app.services.graphrag.graphrag_engine import GraphRAGEngine

            engine = GraphRAGEngine()
            result = await engine.generate_answer(
                query=user_input,
                collection_name=collection_name,
                system_prompt=conversation_context
            )

            answer_text = result.get("answer", "")
            sources = result.get("sources", [])

            if not answer_text:
                answer_text = "抱歉，我暂时无法回答这个问题。"

            # 在回复末尾嵌入脚注式溯源
            if sources:
                footnotes = "\n\n---\n**参考来源：**\n"
                for i, source in enumerate(sources, 1):
                    file_name = source.get("file_name", "未知文档")
                    page = source.get("page_number")
                    offset = source.get("char_offset", 0)
                    if page:
                        footnotes += f"[{i}] {file_name}，第{page}页，偏移量{offset}\n"
                    else:
                        footnotes += f"[{i}] {file_name}，偏移量{offset}\n"
                answer_text += footnotes

            # 添加助手消息到记忆
            await conversation_memory.add_message(session_id, "assistant", answer_text, {
                "collection_used": collection_name,
                "sources": sources
            })

            # 构建返回结果
            session_stats = conversation_memory.get_session_stats(session_id)

            return {
                "success": True,
                "response": answer_text,
                "session_id": session_id,
                "conversation_length": session_stats.get("total_messages", 0),
                "timestamp": session_stats.get("last_message_time"),
                "mode": "rag",
                "sources": sources
            }

        except Exception as e:
            logger.error(f"Agent对话失败: {e}", exc_info=True)

            # RAG 失败时降级到通用助手
            logger.warning(f"RAG对话失败，降级到通用助手: {e}")
            return await self.chat_general(user_input, session_id, **kwargs)

    async def _chat_multimodal(self, user_input: str, session_id: str,
                               image_paths: List[str]) -> Dict[str, Any]:
        """多模态图文对话处理。"""
        from app.core.llm_client import call_multimodal_chat, is_llm_configured

        try:
            logger.info(f"多模态对话请求 - 会话: {session_id}, 图片数: {len(image_paths)}")

            await conversation_memory.create_session(session_id)
            # 记忆中以文本形式记录（图片引用路径）
            await conversation_memory.add_message(
                session_id, "user",
                f"[图片消息 ×{len(image_paths)}] {user_input or '请描述图片内容'}"
            )

            # 获取对话上下文（多模态模型同样需要上下文）
            conversation_context = await conversation_memory.get_conversation_context(session_id)

            if not is_llm_configured("aliyun_vision"):
                logger.warning("多模态模型未配置，使用 Mock 数据")
                from app.core.mock_data import get_mock_data
                response_text = get_mock_data("multimodal_chat_response",
                                              user_input=user_input, image_count=len(image_paths))
            else:
                # 构建系统提示词
                system_prompt = (
                    "你是 KQuery 智能助手的多模态版本，能够理解用户上传的图片内容。"
                    "请基于图片内容和用户问题给出准确、详细的回答。"
                    "如果图片中包含图表、数据或文字，请仔细解读并引用。"
                )
                # 将上下文拼入 user_input
                contextual_input = user_input or "请描述这张图片的内容"
                if conversation_context:
                    contextual_input = f"{system_prompt}\n\n对话历史：\n{conversation_context}\n\n当前问题：{contextual_input}"
                else:
                    contextual_input = f"{system_prompt}\n\n当前问题：{contextual_input}"

                response_text = await call_multimodal_chat(
                    text=contextual_input,
                    image_paths=image_paths,
                    model=settings.llm.multimodal_model_name or settings.aliyun.vision_model,
                    api_key=settings.llm.multimodal_api_key or settings.aliyun.access_key_secret,
                    provider=settings.llm.multimodal_provider,
                    mock_module="multimodal_chat_response",
                )

            if not response_text:
                response_text = "抱歉，我暂时无法分析这些图片。"

            await conversation_memory.add_message(
                session_id, "assistant", response_text,
                {"mode": "multimodal", "image_count": len(image_paths)}
            )

            session_stats = conversation_memory.get_session_stats(session_id)
            return {
                "success": True,
                "response": response_text,
                "session_id": session_id,
                "conversation_length": session_stats.get("total_messages", 0),
                "timestamp": session_stats.get("last_message_time"),
                "mode": "multimodal",
                "tools_used": [],
                "sources": [],
            }

        except Exception as e:
            logger.error(f"多模态对话失败: {e}", exc_info=True)
            error_response = "抱歉，图片分析过程中出现错误。请稍后重试或尝试用文字描述您的问题。"
            await conversation_memory.add_message(session_id, "assistant", error_response, {"error": str(e)})
            return {
                "success": False,
                "response": error_response,
                "error": str(e),
                "session_id": session_id,
                "mode": "multimodal",
                "tools_used": [],
                "sources": [],
            }

    async def process_document(self,

                              file_path: str,
                              session_id: str,
                              collection_name: str = "default",
                              **kwargs) -> Dict[str, Any]:
        """处理用户上传的文档

        Args:
            file_path: 文件路径
            session_id: 会话ID
            collection_name: 目标集合名称
            **kwargs: 其他参数

        Returns:
            处理结果
        """
        try:
            logger.info(f"文档处理请求 - 会话: {session_id}, 文件: {file_path}")

            # 确保会话存在
            await conversation_memory.create_session(session_id)

            # 使用文档处理工具
            if "process_document" in [tool.name for tool in self.tools]:
                from app.agents.rag_tool import DocumentProcessingTool
                tool = DocumentProcessingTool()

                result = await tool._arun(
                    file_path=file_path,
                    collection_name=collection_name,
                    chunk_size=kwargs.get("chunk_size", 500),
                    chunk_overlap=kwargs.get("chunk_overlap", 50)
                )

                # 添加处理结果到对话历史
                await conversation_memory.add_message(session_id, "assistant", result, {
                    "action": "document_processing",
                    "file_path": file_path,
                    "collection_name": collection_name
                })

                return {
                    "success": True,
                    "response": result,
                    "session_id": session_id,
                    "file_processed": file_path,
                    "collection_name": collection_name
                }
            else:
                error_msg = "文档处理功能未启用"
                await conversation_memory.add_message(session_id, "assistant", error_msg)

                return {
                    "success": False,
                    "response": error_msg,
                    "error": "Document processing tool not available"
                }

        except Exception as e:
            logger.error(f"文档处理失败: {e}", exc_info=True)

            error_response = f"文档处理失败: {str(e)}"
            await conversation_memory.add_message(session_id, "assistant", error_response)

            return {
                "success": False,
                "response": error_response,
                "error": str(e)
            }

    def get_session_info(self, session_id: str) -> Dict[str, Any]:
        """获取会话信息

        Args:
            session_id: 会话ID

        Returns:
            会话信息
        """
        return conversation_memory.get_session_stats(session_id)

    def clear_session(self, session_id: str) -> bool:
        """清空会话历史

        Args:
            session_id: 会话ID

        Returns:
            是否成功
        """
        try:
            asyncio.get_event_loop().run_until_complete(
                conversation_memory.clear_session(session_id)
            )
            return True
        except Exception as e:
            logger.error(f"清空会话失败: {e}")
            return False


def get_qa_agent(
    model_name: str = None,
    temperature: float = 0.1,
    max_iterations: int = 5,
    enable_rag: bool = True,
) -> QAAgent:
    """获取或创建QA Agent实例的工厂函数。

    使用此函数代替直接创建全局实例，以支持延迟初始化。

    Args:
        model_name: 使用的LLM模型名称
        temperature: 模型温度参数
        max_iterations: 最大推理迭代次数
        enable_rag: 是否启用RAG功能

    Returns:
        QAAgent: Agent实例
    """
    return QAAgent(
        model_name=model_name,
        temperature=temperature,
        max_iterations=max_iterations,
        enable_rag=enable_rag,
    )


_default_agent: Optional[QAAgent] = None


def get_default_agent() -> QAAgent:
    """获取默认Agent实例（延迟初始化）。"""
    global _default_agent
    if _default_agent is None:
        _default_agent = QAAgent()
    return _default_agent
