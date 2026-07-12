"""Gradio前端应用 - 提供用户友好的Web界面。

包含文件上传、智能问答和对话管理功能。
"""

import gradio as gr
import os
import tempfile
import uuid
from typing import Dict, Any, Optional
from datetime import datetime
from app.core.settings import get_settings
from app.core.logger import get_logger
from app.agents.qa_agent import get_default_agent

logger = get_logger(__name__)


class GradioApp:
    """Gradio前端应用类"""
    
    def __init__(self):
        """初始化Gradio应用"""
        self.app = None
        self.supported_file_types = [
            # 文本格式
            ".pdf", ".docx", ".doc", ".xlsx", ".xls", 
            ".pptx", ".ppt", ".html", ".htm", ".md", 
            ".markdown", ".txt",
            # 图片格式
            ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".gif", ".webp",
        ]
        
        # 会话管理
        self.active_sessions: Dict[str, Dict[str, Any]] = {}
        
        logger.info("Gradio应用初始化完成")
    
    def _generate_session_id(self) -> str:
        """生成唯一的会话ID"""
        return str(uuid.uuid4())
    
    def _get_or_create_session(self, session_id: Optional[str] = None) -> str:
        """获取或创建会话"""
        if not session_id or session_id not in self.active_sessions:
            session_id = self._generate_session_id()
            self.active_sessions[session_id] = {
                "created_at": datetime.now(),
                "message_count": 0,
                "last_activity": datetime.now(),
                "collection_name": "default"
            }
            logger.info(f"创建新会话: {session_id}")
        else:
            # 更新活动时间
            self.active_sessions[session_id]["last_activity"] = datetime.now()
        
        return session_id
    
    async def handle_chat(self, 
                         user_input: str, 
                         session_id: Optional[str],
                         collection_name: str = "default") -> tuple[str, str, list[list]]:
        """处理用户聊天请求
        
        Args:
            user_input: 用户输入
            session_id: 会话ID
            collection_name: 知识库集合名称
            
        Returns:
            (响应内容, 会话ID, 聊天历史)
        """
        try:
            # 获取或创建会话
            session_id = self._get_or_create_session(session_id)
            
            # 更新会话信息
            self.active_sessions[session_id]["collection_name"] = collection_name
            self.active_sessions[session_id]["message_count"] += 1
            
            # 调用Agent进行对话
            result = await get_default_agent().chat(
                user_input=user_input,
                session_id=session_id,
                collection_name=collection_name
            )
            
            # 获取对话历史
            history = get_default_agent().get_session_info(session_id)
            chat_history = self._format_chat_history(session_id)
            
            if result["success"]:
                logger.info(f"聊天处理完成 - 会话: {session_id}, 消息长度: {len(result['response'])}")
                return result["response"], session_id, chat_history
            else:
                error_msg = f"处理失败: {result.get('error', '未知错误')}"
                logger.error(f"聊天处理失败 - 会话: {session_id}, 错误: {error_msg}")
                return error_msg, session_id, chat_history
                
        except Exception as e:
            logger.error(f"聊天处理异常: {e}", exc_info=True)
            error_msg = f"系统错误: {str(e)}"
            session_id = self._get_or_create_session(session_id)
            return error_msg, session_id, []
    
    async def handle_file_upload(self, 
                                file, 
                                session_id: Optional[str],
                                collection_name: str = "default") -> tuple[str, str]:
        """处理文件上传
        
        Args:
            file: 上传的文件对象
            session_id: 会话ID
            collection_name: 目标集合名称
            
        Returns:
            (处理结果消息, 会话ID)
        """
        try:
            # 获取或创建会话
            session_id = self._get_or_create_session(session_id)
            
            if not file:
                return "请选择要上传的文件", session_id
            
            # 检查文件类型
            file_ext = os.path.splitext(file.name)[1].lower()
            if file_ext not in self.supported_file_types:
                return f"不支持的文件类型: {file_ext}，支持的类型: {', '.join(self.supported_file_types)}", session_id
            
            # 使用配置中的上传目录
            from app.core.settings import get_settings
            _settings = get_settings()
            
            # 确保上传目录存在
            os.makedirs(_settings.storage.upload_dir, exist_ok=True)
            
            # 构建文件保存路径
            import uuid
            safe_filename = f"{session_id}_{uuid.uuid4().hex[:8]}{file_ext}"
            file_path = os.path.join(_settings.storage.upload_dir, safe_filename)
            
            # 保存文件到指定目录
            with open(file_path, "wb") as f:
                content = file.read()
                f.write(content)
            
            logger.info(f"文件已保存到: {file_path}")
            
            try:
                # 调用Agent处理文档
                result = await default_qa_agent.process_document(
                    file_path=file_path,
                    session_id=session_id,
                    collection_name=collection_name
                )
                
                # 如果配置了自动清理，则删除原文件
                if _settings.storage.auto_cleanup:
                    try:
                        os.unlink(file_path)
                        logger.info(f"文件已自动清理: {file_path}")
                    except Exception as cleanup_error:
                        logger.warning(f"文件清理失败: {cleanup_error}")
                
                if result["success"]:
                    logger.info(f"文件处理成功 - 会话: {session_id}, 文件: {file.name}")
                    
                    # 更新会话信息
                    self.active_sessions[session_id]["collection_name"] = collection_name
                    
                    # 根据配置生成成功消息
                    if _settings.storage.auto_cleanup:
                        success_msg = f"文件 '{file.name}' 已成功处理并添加到知识库 '{collection_name}'。您现在可以询问关于这个文件的问题了。"
                    else:
                        success_msg = f"文件 '{file.name}' 已成功处理并保存到 {file_path}。您现在可以询问关于这个文件的问题了。"
                    
                    return success_msg, session_id
                else:
                    error_msg = f"文件处理失败: {result.get('response', '未知错误')}"
                    logger.error(f"文件处理失败 - 会话: {session_id}, 文件: {file.name}, 错误: {error_msg}")
                    return error_msg, session_id
                    
            except Exception as e:
                # 确保文件被清理
                try:
                    os.unlink(file_path)
                except:
                    pass
                raise e
                
        except Exception as e:
            logger.error(f"文件上传处理异常: {e}", exc_info=True)
            error_msg = f"文件上传失败: {str(e)}"
            session_id = self._get_or_create_session(session_id)
            return error_msg, session_id
    
    def _format_chat_history(self, session_id: str) -> list[list]:
        """格式化聊天历史用于Gradio显示
        
        Args:
            session_id: 会话ID
            
        Returns:
            格式化的聊天历史
        """
        try:
            history_data = default_qa_agent.get_session_info(session_id)
            
            # 获取对话历史
            from app.agents.memory import conversation_memory
            messages = conversation_memory.get_conversation_history(session_id)
            
            chat_history = []
            for msg in messages:
                if msg["role"] == "user":
                    chat_history.append([msg["content"], None])
                elif msg["role"] == "assistant":
                    if chat_history and chat_history[-1][1] is None:
                        chat_history[-1][1] = msg["content"]
                    else:
                        chat_history.append([None, msg["content"]])
            
            return chat_history
            
        except Exception as e:
            logger.error(f"格式化聊天历史失败: {e}")
            return []
    
    def clear_conversation(self, session_id: str) -> tuple[str, str, list]:
        """清空对话历史
        
        Args:
            session_id: 会话ID
            
        Returns:
            (消息, 新会话ID, 空历史)
        """
        try:
            # 清空Agent的对话历史
            get_default_agent().clear_session(session_id)
            
            # 创建新会话
            new_session_id = self._generate_session_id()
            self.active_sessions[new_session_id] = {
                "created_at": datetime.now(),
                "message_count": 0,
                "last_activity": datetime.now(),
                "collection_name": "default"
            }
            
            logger.info(f"清空对话历史 - 旧会话: {session_id}, 新会话: {new_session_id}")
            return "对话历史已清空", new_session_id, []
            
        except Exception as e:
            logger.error(f"清空对话历史失败: {e}")
            return f"清空失败: {str(e)}", session_id, []
    
    def get_session_info(self, session_id: str) -> str:
        """获取会话信息
        
        Args:
            session_id: 会话ID
            
        Returns:
            会话信息字符串
        """
        try:
            if session_id in self.active_sessions:
                session_info = self.active_sessions[session_id]
                agent_stats = get_default_agent().get_session_info(session_id)
                
                info_lines = [
                    f"会话ID: {session_id}",
                    f"创建时间: {session_info['created_at'].strftime('%Y-%m-%d %H:%M:%S')}",
                    f"消息数量: {session_info['message_count']}",
                    f"最后活动: {session_info['last_activity'].strftime('%Y-%m-%d %H:%M:%S')}",
                    f"知识库: {session_info.get('collection_name', 'default')}",
                    f"对话轮数: {agent_stats.get('total_messages', 0)}",
                    f"用户消息: {agent_stats.get('user_messages', 0)}",
                    f"助手消息: {agent_stats.get('assistant_messages', 0)}"
                ]
                
                return "\n".join(info_lines)
            else:
                return "会话不存在"
                
        except Exception as e:
            return f"获取会话信息失败: {str(e)}"
    
    def create_interface(self) -> gr.Blocks:
        """创建Gradio界面"""
        
        with gr.Blocks(
            title="KQuery智能问答系统",
            theme=gr.themes.Soft(),
            css="""
            .chat-container {
                max-height: 500px;
                overflow-y: auto;
            }
            .file-upload {
                border: 2px dashed #ccc;
                padding: 20px;
                text-align: center;
            }
            """
        ) as interface:
            
            gr.Markdown("""
            # 🤖 KQuery智能问答系统
            
            基于AI的智能文档问答系统，支持多种文件格式上传和自然语言对话。
            """)
            
            # 会话状态管理
            session_state = gr.State(value="")
            
            with gr.Row():
                with gr.Column(scale=1):
                    # 文件上传区域
                    gr.Markdown("## 📁 文件上传")
                    
                    file_upload = gr.File(
                        label="上传文档",
                        file_types=self.supported_file_types,
                        file_count="single",
                        elem_classes="file-upload"
                    )
                    
                    collection_name = gr.Textbox(
                        label="知识库名称",
                        value="default",
                        placeholder="输入知识库集合名称",
                        info="文档将被添加到指定的知识库中"
                    )
                    
                    upload_btn = gr.Button("上传并处理", variant="primary")
                    upload_result = gr.Textbox(
                        label="上传结果",
                        interactive=False,
                        show_label=True
                    )
                    
                    gr.Markdown("---")
                    
                    # 会话管理
                    gr.Markdown("## 🔧 会话管理")
                    
                    clear_btn = gr.Button("清空对话", variant="secondary")
                    session_info_btn = gr.Button("会话信息", variant="secondary")
                    session_info_display = gr.Textbox(
                        label="会话信息",
                        interactive=False,
                        lines=8
                    )
                
                with gr.Column(scale=2):
                    # 聊天区域
                    gr.Markdown("## 💬 智能问答")
                    
                    chatbot = gr.Chatbot(
                        label="对话历史",
                        height=400,
                        show_label=False,
                        elem_classes="chat-container"
                    )
                    
                    with gr.Row():
                        user_input = gr.Textbox(
                            label="输入问题",
                            placeholder="请输入您的问题...",
                            show_label=False,
                            scale=4
                        )
                        
                        chat_collection = gr.Textbox(
                            label="查询知识库",
                            value="default",
                            placeholder="知识库名称",
                            show_label=False,
                            scale=1
                        )
                        
                        submit_btn = gr.Button("发送", variant="primary", scale=1)
            
            # 事件处理
            # 文件上传处理
            upload_btn.click(
                fn=self.handle_file_upload,
                inputs=[file_upload, session_state, collection_name],
                outputs=[upload_result, session_state]
            )
            
            # 聊天处理
            submit_btn.click(
                fn=self.handle_chat,
                inputs=[user_input, session_state, chat_collection],
                outputs=[user_input, session_state, chatbot]
            ).then(
                lambda: "", None, user_input
            )
            
            # 清空对话
            clear_btn.click(
                fn=self.clear_conversation,
                inputs=[session_state],
                outputs=[upload_result, session_state, chatbot]
            )
            
            # 会话信息
            session_info_btn.click(
                fn=self.get_session_info,
                inputs=[session_state],
                outputs=[session_info_display]
            )
            
            # 初始化会话
            def initialize_session():
                initial_session = self._get_or_create_session()
                return initial_session, []
            
            interface.load(
                fn=initialize_session,
                outputs=[session_state, chatbot]
            )
            
            # 示例和说明
            gr.Markdown("""
            ## 📝 使用说明
            
            1. **上传文档**: 支持PDF、Word、Excel、PPT、HTML、Markdown、Text等格式
            2. **智能问答**: 基于上传的文档内容进行问答
            3. **多知识库**: 可以为不同主题创建不同的知识库
            4. **对话记忆**: 系统会记住之前的对话内容
            
            ## 💡 示例问题
            
            - "请总结一下这个文档的主要内容"
            - "文档中提到了哪些关键技术点？"
            - "帮我解释一下这个概念"
            - "这个方案的优缺点是什么？"
            """)
        
        return interface
    
    def launch(self, 
               server_name: str = "0.0.0.0",
               server_port: int = 7860,
               share: bool = False,
               **kwargs) -> None:
        """启动Gradio应用
        
        Args:
            server_name: 服务器地址
            server_port: 服务器端口
            share: 是否共享链接
            **kwargs: 其他参数
        """
        if not self.app:
            self.app = self.create_interface()
        
        logger.info(f"启动Gradio界面 - 地址: {server_name}:{server_port}")
        
        self.app.launch(
            server_name=server_name,
            server_port=server_port,
            share=share,
            **kwargs
        )


def create_gradio_app() -> GradioApp:
    """创建Gradio应用实例"""
    return GradioApp()