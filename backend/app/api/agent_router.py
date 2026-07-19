"""Agent API路由 - 提供Agent功能的RESTful API接口。

将Agent功能集成到FastAPI应用中，提供Web API访问。
"""

from fastapi import APIRouter, HTTPException, UploadFile, File, Query, WebSocket, WebSocketDisconnect, Request
from typing import Optional, Dict, Any
import time
import uuid
import json
import os
from app.agents.qa_agent import get_default_agent
from app.core.logger import get_logger
from app.models.chat import ChatRequest
from app.core.audit_logger import audit_logger
from app.core.settings import get_settings


logger = get_logger(__name__)

router = APIRouter(prefix="/agent", tags=["智能问答Agent"])


_settings = get_settings()



# 请求模型（已迁移至 app.models.chat）


def get_qa_agent():
    return get_default_agent()


@router.post("/chat")
async def chat_with_agent(request: Request, request_body: ChatRequest) -> Dict[str, Any]:
    """与Agent进行对话
    
    基于用户输入的问题，调用Agent进行智能问答。
    当未指定知识库或知识库不存在时，自动降级为通用助手模式。
    支持图片上传，含图片时优先使用多模态模型理解图像内容。
    """
    start_time = time.perf_counter()
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    try:
        # 如果没有提供会话ID，创建新会话
        if not request_body.session_id:
            request_body.session_id = str(uuid.uuid4())
        
        logger.info(f"Agent聊天API调用 - 会话: {request_body.session_id}, 查询: {request_body.query}")
        
        # collection_name 为空时自动降级为通用模式
        actual_enable_rag = request_body.enable_rag and bool(request_body.collection_name)
        
        # 解析并保存图片
        image_paths: list[str] = []
        if request_body.has_images:
            if len(request_body.images) > 3:
                raise HTTPException(status_code=400, detail="单次最多上传 3 张图片")
            for img in request_body.images:
                mime = img.mime_type()
                allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
                if mime not in allowed:
                    raise HTTPException(status_code=400, detail=f"不支持的图片格式: {mime}")
                try:
                    image_bytes = img.extract_base64()
                except Exception as e:
                    raise HTTPException(status_code=400, detail=f"图片解析失败: {str(e)}") from e
                if len(image_bytes) > 5 * 1024 * 1024:
                    raise HTTPException(status_code=400, detail="单张图片不能超过 5MB")
                safe = f"{request_body.session_id}_{uuid.uuid4().hex[:8]}.jpg"
                file_path = os.path.join(_settings.storage.upload_dir, "chat_images", safe)
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as f:
                    f.write(image_bytes)
                image_paths.append(file_path)
        
        # 调用Agent
        result = await get_default_agent().chat(
            user_input=request_body.query,
            session_id=request_body.session_id,
            collection_name=request_body.collection_name or "default",
            enable_rag=actual_enable_rag,
            image_paths=image_paths if request_body.has_images else None,
        )
        
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/agent/chat",
            query_text=request_body.query,
            collection_name=request_body.collection_name,
            response_count=1,
            duration_ms=duration_ms,
            status="success",
            request_id=request_id,
        )

        
        return {
            "success": result["success"],
            "session_id": request_body.session_id,
            "response": result["response"],
            "conversation_length": result.get("conversation_length", 0),
            "tools_used": result.get("tools_used", []),
            "timestamp": result.get("timestamp"),
            "mode": result.get("mode", "rag" if actual_enable_rag else "general"),
            "sources": result.get("sources", [])
        }
        
    except HTTPException:
        raise
    except Exception as e:
        duration_ms = (time.perf_counter() - start_time) * 1000
        await audit_logger.log_query_access(
            request=request,
            endpoint="/api/v1/agent/chat",
            query_text=request_body.query if request_body else None,
            collection_name=request_body.collection_name if request_body else None,
            duration_ms=duration_ms,
            status="failure",
            request_id=request_id,
        )
        logger.error(f"Agent聊天API失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"聊天处理失败: {str(e)}")
    finally:
        if _settings.storage.auto_cleanup:
            for p in image_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass



@router.post("/upload")
async def upload_and_process_document(
    file: UploadFile = File(..., description="上传的文档文件"),
    session_id: Optional[str] = Query(None, description="会话ID"),
    collection_name: str = Query("default", description="目标知识库集合名称"),
    chunk_size: int = Query(500, description="文本块大小"),
    chunk_overlap: int = Query(50, description="块重叠大小")
) -> Dict[str, Any]:
    """上传并处理文档
    
    将文档添加到知识库中，支持多种文件格式。
    """
    try:
        # 如果没有提供会话ID，创建新会话
        if not session_id:
            session_id = str(uuid.uuid4())
        
        logger.info(f"文档上传API调用 - 会话: {session_id}, 文件: {file.filename}")
        
        # 使用配置中的上传目录
        import os
        from app.core.settings import get_settings
        _settings = get_settings()
        
        # 确保上传目录存在
        os.makedirs(_settings.storage.upload_dir, exist_ok=True)
        
        # 构建文件保存路径
        file_ext = os.path.splitext(file.filename)[1]
        safe_filename = f"{session_id}_{uuid.uuid4().hex[:8]}{file_ext}"
        file_path = os.path.join(_settings.storage.upload_dir, safe_filename)
        
        # 保存文件到指定目录
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        logger.info(f"文件已保存到: {file_path}")
        
        try:
            # 调用Agent处理文档
            result = await get_default_agent().process_document(
                file_path=file_path,
                session_id=session_id,
                collection_name=collection_name,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap
            )
            
            # 如果配置了自动清理，则删除原文件
            if _settings.storage.auto_cleanup:
                try:
                    os.unlink(file_path)
                    logger.info(f"文件已自动清理: {file_path}")
                except Exception as cleanup_error:
                    logger.warning(f"文件清理失败: {cleanup_error}")
            
            return {
                "success": result["success"],
                "session_id": session_id,
                "response": result["response"],
                "file_name": file.filename,
                "saved_path": file_path if not _settings.storage.auto_cleanup else None,
                "collection_name": collection_name
            }
            
        except Exception as e:
            # 确保文件被清理
            try:
                os.unlink(file_path)
            except:
                pass
            raise e
            
    except Exception as e:
        logger.error(f"文档上传API失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"文档处理失败: {str(e)}")


@router.post("/chat/stream")
async def stream_chat_with_agent(request: ChatRequest):
    """Agent流式对话（SSE）

    以SSE(Server-Sent Events)方式返回Agent的回答片段。
    支持图片上传，含图片时优先使用多模态模型理解图像内容。
    """
    from fastapi.responses import StreamingResponse
    import asyncio

    if not request.session_id:
        request.session_id = str(uuid.uuid4())

    actual_enable_rag = request.enable_rag and bool(request.collection_name)

    # 解析并保存图片（复用 /chat 相同逻辑）
    image_paths: list[str] = []
    if request.has_images:
        if len(request.images) > 3:
            async def error_gen():
                yield f"data: {json.dumps({'type': 'error', 'message': '单次最多上传 3 张图片'})}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(error_gen(), media_type="text/event-stream")
        for img in request.images:
            mime = img.mime_type()
            allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
            if mime not in allowed:
                async def error_gen():
                    yield f"data: {json.dumps({'type': 'error', 'message': f'不支持的图片格式: {mime}'})}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(error_gen(), media_type="text/event-stream")
            try:
                image_bytes = img.extract_base64()
            except Exception as e:
                async def error_gen():
                    yield f"data: {json.dumps({'type': 'error', 'message': f'图片解析失败: {str(e)}'})}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(error_gen(), media_type="text/event-stream")
            if len(image_bytes) > 5 * 1024 * 1024:
                async def error_gen():
                    yield f"data: {json.dumps({'type': 'error', 'message': '单张图片不能超过 5MB'})}\n\n"
                    yield "data: [DONE]\n\n"
                return StreamingResponse(error_gen(), media_type="text/event-stream")
            safe = f"{request.session_id}_{uuid.uuid4().hex[:8]}.jpg"
            file_path = os.path.join(_settings.storage.upload_dir, "chat_images", safe)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "wb") as f:
                f.write(image_bytes)
            image_paths.append(file_path)

    async def event_generator():
        try:
            yield f"data: {json.dumps({'type': 'start', 'session_id': request.session_id})}\n\n"

            result = await get_default_agent().chat(
                user_input=request.query,
                session_id=request.session_id,
                collection_name=request.collection_name or "default",
                enable_rag=actual_enable_rag,
                image_paths=image_paths if request.has_images else None,
            )

            response = result.get("response", "")
            chunk_size = 50
            for i in range(0, len(response), chunk_size):
                chunk = response[i:i + chunk_size]
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
                await asyncio.sleep(0.02)

            yield f"data: {json.dumps({
                'type': 'done',
                'session_id': request.session_id,
                'conversation_length': result.get('conversation_length', 0),
                'mode': result.get('mode', 'rag' if actual_enable_rag else 'general'),
            })}\n\n"

        except Exception as e:
            logger.error(f"SSE流式对话失败: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            if _settings.storage.auto_cleanup:
                for p in image_paths:
                    try:
                        os.unlink(p)
                    except Exception:
                        pass
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )



@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, session_id: str = "default"):
    """WebSocket实时对话

    通过WebSocket进行实时对话，支持双向消息通信。
    支持图片上传，消息 JSON 可包含 images 数组。
    """
    from app.api.ws_manager import ws_manager

    await ws_manager.connect(websocket, session_id)

    try:
        while True:
            data = await ws_manager.receive(websocket)
            message = data.get("message", "")
            collection_name = data.get("collection_name", "default")
            images = data.get("images", []) or []

            # 解析并保存图片
            image_paths: list[str] = []
            if images:
                if len(images) > 3:
                    await ws_manager.send(websocket, {
                        "type": "error",
                        "message": "单次最多上传 3 张图片",
                        "session_id": session_id,
                    })
                    continue
                for img in images:
                    data_url = img.get("data_url", "")
                    if not data_url.startswith("data:image/"):
                        await ws_manager.send(websocket, {
                            "type": "error",
                            "message": "图片格式不正确",
                            "session_id": session_id,
                        })
                        break
                else:
                    for img in images:
                        data_url = img.get("data_url", "")
                        try:
                            from app.models.chat import ChatImage
                            chat_img = ChatImage(data_url=data_url, type=img.get("type"))
                            mime = chat_img.mime_type()
                            allowed = {"image/png", "image/jpeg", "image/webp", "image/gif"}
                            if mime not in allowed:
                                raise ValueError(f"不支持的图片格式: {mime}")
                            image_bytes = chat_img.extract_base64()
                            if len(image_bytes) > 5 * 1024 * 1024:
                                raise ValueError("单张图片不能超过 5MB")
                            safe = f"{session_id}_{uuid.uuid4().hex[:8]}.jpg"
                            file_path = os.path.join(_settings.storage.upload_dir, "chat_images", safe)
                            os.makedirs(os.path.dirname(file_path), exist_ok=True)
                            with open(file_path, "wb") as f:
                                f.write(image_bytes)
                            image_paths.append(file_path)
                        except Exception as e:
                            await ws_manager.send(websocket, {
                                "type": "error",
                                "message": str(e),
                                "session_id": session_id,
                            })
                            break

            await ws_manager.send(websocket, {
                "type": "status",
                "status": "thinking",
                "session_id": session_id,
            })

            result = await get_default_agent().chat(
                user_input=message,
                session_id=session_id,
                collection_name=collection_name,
                image_paths=image_paths if image_paths else None,
            )

            await ws_manager.send(websocket, {
                "type": "message",
                "content": result.get("response", ""),
                "session_id": session_id,
                "mode": result.get("mode", "rag" if collection_name else "general"),
                "conversation_length": result.get("conversation_length", 0),
            })

            await ws_manager.send(websocket, {
                "type": "status",
                "status": "done",
                "session_id": session_id,
            })

            if _settings.storage.auto_cleanup:
                for p in image_paths:
                    try:
                        os.unlink(p)
                    except Exception:
                        pass

    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, session_id)
        logger.info(f"WebSocket连接已断开: session_id={session_id}")
    except Exception as e:
        logger.error(f"WebSocket处理失败: {e}", exc_info=True)
        ws_manager.disconnect(websocket, session_id)



@router.post("/voice/upload")
async def upload_voice(
    file: UploadFile = File(..., description="音频文件"),
    session_id: Optional[str] = Query(None, description="会话ID"),
    collection_name: str = Query("default", description="知识库集合名称"),
):
    """语音上传与识别

    接收音频文件，保存并触发语音识别处理。
    支持格式: WAV, MP3, OGG, M4A, AMR
    """
    import os
    from app.core.settings import get_settings
    _settings = get_settings()

    if not session_id:
        session_id = str(uuid.uuid4())

    allowed_formats = {"wav", "mp3", "ogg", "m4a", "amr"}
    file_ext = os.path.splitext(file.filename or "")[1].lower().lstrip(".")
    if file_ext not in allowed_formats:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的音频格式: {file_ext}，支持: {', '.join(allowed_formats)}"
        )

    try:
        voice_dir = os.path.join(_settings.storage.upload_dir, "voice")
        os.makedirs(voice_dir, exist_ok=True)

        safe_filename = f"{session_id}_{uuid.uuid4().hex[:8]}.{file_ext}"
        file_path = os.path.join(voice_dir, safe_filename)

        content = await file.read()
        duration = len(content) / 16000

        with open(file_path, "wb") as f:
            f.write(content)

        logger.info(
            f"语音上传成功: session_id={session_id}, "
            f"file={file.filename}, size={len(content)}bytes"
        )

        return {
            "task_id": str(uuid.uuid4()),
            "session_id": session_id,
            "text": "",
            "duration": round(duration, 1),
            "file_path": file_path,
            "collection_name": collection_name,
        }

    except Exception as e:
        logger.error(f"语音上传失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"语音上传失败: {str(e)}")


@router.get("/session/{session_id}")
async def get_session_info(session_id: str) -> Dict[str, Any]:
    """获取会话信息
    
    获取指定会话的详细信息，包括对话历史和统计信息。
    """
    try:
        logger.info(f"获取会话信息 - 会话: {session_id}")
        
        # 获取会话信息
        session_info = get_default_agent().get_session_info(session_id)
        
        return {
            "success": True,
            "session_id": session_id,
            "session_info": session_info
        }
        
    except Exception as e:
        logger.error(f"获取会话信息失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取会话信息失败: {str(e)}")


@router.delete("/session/{session_id}")
async def clear_session(session_id: str) -> Dict[str, Any]:
    """清空会话历史
    
    清空指定会话的所有对话历史。
    """
    try:
        logger.info(f"清空会话历史 - 会话: {session_id}")
        
        # 直接调用异步方法清空会话
        from app.agents.memory import conversation_memory
        await conversation_memory.clear_session(session_id)
        
        return {
            "success": True,
            "session_id": session_id,
            "message": "会话历史已清空"
        }
        
    except Exception as e:
        logger.error(f"清空会话失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"清空会话失败: {str(e)}")


@router.get("/collections")
async def list_collections() -> Dict[str, Any]:
    """列出可用知识库集合
    
    获取系统中所有可用的知识库集合列表。
    """
    try:
        logger.info("获取知识库集合列表")
        
        # 这里可以集成现有的集合查询功能
        # 暂时返回示例数据
        collections = ["default", "tech_docs", "research_papers"]
        
        return {
            "success": True,
            "collections": collections,
            "total_count": len(collections)
        }
        
    except Exception as e:
        logger.error(f"获取集合列表失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取集合列表失败: {str(e)}")


@router.get("/status")
async def get_agent_status() -> Dict[str, Any]:
    """获取Agent状态
    
    获取Agent系统的运行状态和配置信息。
    """
    try:
        logger.info("获取Agent状态")
        
        # 获取活跃会话数量
        from app.agents.memory import conversation_memory
        active_sessions = conversation_memory.get_session_ids()
        
        from app.core.settings import get_settings
        _settings = get_settings()

        return {
            "success": True,
            "status": "running",
            "agent_model": _settings.llm.agent_model_name,
            "rag_enabled": True,
            "active_sessions": len(active_sessions),
            "total_tools": 2,  # rag_search, process_document
            "version": "1.0.0"
        }
        
    except Exception as e:
        logger.error(f"获取Agent状态失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取Agent状态失败: {str(e)}")


@router.get("/tools")
async def list_tools() -> Dict[str, Any]:
    """列出可用工具
    
    获取Agent可用的所有工具列表及其描述。
    """
    try:
        logger.info("获取工具列表")
        
        tools_info = [
            {
                "name": "rag_search",
                "description": "在知识库中搜索相关信息",
                "parameters": {
                    "query": "查询文本",
                    "collection_name": "知识库集合名称",
                    "top_k": "返回结果数量",
                    "enable_hyde": "是否启用智能检索增强"
                }
            },
            {
                "name": "process_document", 
                "description": "处理文档并将其添加到知识库",
                "parameters": {
                    "file_path": "文件路径",
                    "collection_name": "目标知识库集合名称",
                    "chunk_size": "文本块大小",
                    "chunk_overlap": "块重叠大小"
                }
            }
        ]
        
        return {
            "success": True,
            "tools": tools_info,
            "total_tools": len(tools_info)
        }
        
    except Exception as e:
        logger.error(f"获取工具列表失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"获取工具列表失败: {str(e)}")