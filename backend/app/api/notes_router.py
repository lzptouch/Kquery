"""笔记管理 API 路由。

提供笔记 CRUD、AI 写作辅助、笔记入库等功能。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.core.logger import get_logger
from app.core.settings import get_settings
from app.models.note import (
    AIWritingRequest,
    NoteCreateRequest,
    NoteListResponse,
    NoteResponse,
    NoteUpdateRequest,
)
from app.services.writing_service import WritingService

logger = get_logger(__name__)

router = APIRouter(prefix="/notes", tags=["笔记与写作"])

settings = get_settings()
NOTES_DIR = Path(settings.storage.upload_dir).parent / "notes"


def _get_notes_dir() -> Path:
    """获取笔记存储目录。"""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    return NOTES_DIR


def _read_note_meta(note_id: str) -> dict:
    """读取笔记元数据。"""
    meta_path = _get_notes_dir() / note_id / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail=f"笔记 {note_id} 不存在")
    with open(meta_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_note_content(note_id: str) -> str:
    """读取笔记内容。"""
    content_path = _get_notes_dir() / note_id / "content.md"
    if not content_path.exists():
        return ""
    with open(content_path, "r", encoding="utf-8") as f:
        return f.read()


def _write_note_meta(note_id: str, meta: dict) -> None:
    """写入笔记元数据。"""
    note_dir = _get_notes_dir() / note_id
    note_dir.mkdir(parents=True, exist_ok=True)
    with open(note_dir / "meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _write_note_content(note_id: str, content: str) -> None:
    """写入笔记内容。"""
    note_dir = _get_notes_dir() / note_id
    note_dir.mkdir(parents=True, exist_ok=True)
    with open(note_dir / "content.md", "w", encoding="utf-8") as f:
        f.write(content)


def _list_all_notes() -> List[dict]:
    """列出所有笔记。"""
    notes_dir = _get_notes_dir()
    notes = []
    if not notes_dir.exists():
        return notes

    for note_dir in notes_dir.iterdir():
        if note_dir.is_dir():
            meta_path = note_dir / "meta.json"
            if meta_path.exists():
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                notes.append(meta)
    # 按更新时间倒序
    notes.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    return notes


@router.get("", response_model=NoteListResponse)
async def list_notes(
    tag: Optional[str] = Query(default=None, description="按标签过滤"),
    collection_name: Optional[str] = Query(default=None, description="按知识库过滤"),
):
    """获取笔记列表。

    Args:
        tag: 可选标签过滤
        collection_name: 可选知识库过滤
    """
    all_notes = _list_all_notes()

    if tag:
        all_notes = [n for n in all_notes if tag in n.get("tags", [])]
    if collection_name:
        all_notes = [n for n in all_notes if n.get("collection_name") == collection_name]

    note_responses = []
    for meta in all_notes:
        note_id = meta.get("id", "")
        content = _read_note_content(note_id)
        note_responses.append(NoteResponse.from_storage(note_id, meta, content))

    return NoteListResponse(notes=note_responses, total=len(note_responses))


@router.post("", response_model=NoteResponse)
async def create_note(request: NoteCreateRequest):
    """创建新笔记。

    Args:
        request: 笔记创建请求
    """
    note_id = str(uuid.uuid4())
    now = datetime.now().isoformat()

    meta = {
        "id": note_id,
        "title": request.title,
        "collection_name": request.collection_name,
        "tags": request.tags,
        "source_type": "user_note",
        "created_at": now,
        "updated_at": now,
    }

    _write_note_meta(note_id, meta)
    _write_note_content(note_id, request.content)

    logger.info(f"笔记已创建: {note_id} - {request.title}")

    return NoteResponse.from_storage(note_id, meta, request.content)


@router.get("/{note_id}", response_model=NoteResponse)
async def get_note(note_id: str):
    """获取笔记详情。

    Args:
        note_id: 笔记 ID
    """
    meta = _read_note_meta(note_id)
    content = _read_note_content(note_id)
    return NoteResponse.from_storage(note_id, meta, content)


@router.put("/{note_id}", response_model=NoteResponse)
async def update_note(note_id: str, request: NoteUpdateRequest):
    """更新笔记。

    Args:
        note_id: 笔记 ID
        request: 更新内容
    """
    meta = _read_note_meta(note_id)

    if request.title is not None:
        meta["title"] = request.title
    if request.collection_name is not None:
        meta["collection_name"] = request.collection_name
    if request.tags is not None:
        meta["tags"] = request.tags
    meta["updated_at"] = datetime.now().isoformat()

    _write_note_meta(note_id, meta)

    if request.content is not None:
        _write_note_content(note_id, request.content)

    content = _read_note_content(note_id)
    logger.info(f"笔记已更新: {note_id}")

    return NoteResponse.from_storage(note_id, meta, content)


@router.delete("/{note_id}")
async def delete_note(note_id: str):
    """删除笔记。

    Args:
        note_id: 笔记 ID
    """
    # 先确认存在
    _read_note_meta(note_id)

    import shutil

    note_dir = _get_notes_dir() / note_id
    shutil.rmtree(note_dir, ignore_errors=True)

    logger.info(f"笔记已删除: {note_id}")
    return {"success": True, "message": f"笔记 {note_id} 已删除"}


@router.post("/{note_id}/ingest")
async def ingest_note_to_kb(note_id: str):
    """将笔记内容写入知识库。

    Args:
        note_id: 笔记 ID
    """
    meta = _read_note_meta(note_id)
    content = _read_note_content(note_id)

    if not content.strip():
        raise HTTPException(status_code=400, detail="笔记内容为空，无法入库")

    try:
        from app.services.rag_service import RAGService

        rag_service = RAGService()
        result = await rag_service.add_text(
            text=content,
            collection_name=meta.get("collection_name", "default"),
            metadata={
                "source_type": "user_note",
                "note_id": note_id,
                "note_title": meta.get("title", ""),
            },
        )

        # 更新笔记 source_type 标记
        meta["source_type"] = "kb_synced"
        meta["updated_at"] = datetime.now().isoformat()
        _write_note_meta(note_id, meta)

        logger.info(f"笔记已入库: {note_id} -> {meta.get('collection_name')}")
        return {"success": True, "message": "笔记已写入知识库", "detail": str(result)}
    except Exception as e:
        logger.error(f"笔记入库失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"入库失败: {str(e)}")


@router.post("/ai/continue")
async def ai_continue_writing(request: AIWritingRequest):
    """AI 续写。

    Args:
        request: 写作请求
    """
    service = WritingService()
    result = await service.continue_writing(text=request.text, style=request.style)
    return {"success": True, "result": result, "action": "continue"}


@router.post("/ai/polish")
async def ai_polish(request: AIWritingRequest):
    """AI 润色。"""
    service = WritingService()
    result = await service.polish(text=request.text, tone=request.style)
    return {"success": True, "result": result, "action": "polish"}


@router.post("/ai/summarize")
async def ai_summarize(request: AIWritingRequest):
    """AI 总结。"""
    service = WritingService()
    result = await service.summarize(text=request.text)
    return {"success": True, "result": result, "action": "summarize"}


@router.post("/ai/translate")
async def ai_translate(request: AIWritingRequest):
    """AI 翻译。"""
    service = WritingService()
    result = await service.translate(text=request.text, target_lang=request.target_lang)
    return {"success": True, "result": result, "action": "translate"}


@router.post("/ai/{action}/stream")
async def ai_writing_stream(action: str, request: AIWritingRequest):
    """AI 写作流式接口。

    Args:
        action: 操作类型 (continue/polish/summarize/translate)
        request: 写作请求
    """
    valid_actions = {"continue", "polish", "summarize", "translate"}
    if action not in valid_actions:
        raise HTTPException(status_code=400, detail=f"不支持的操作: {action}")

    service = WritingService()

    async def generate():
        kwargs = {"text": request.text}
        if action == "continue":
            kwargs["style"] = request.style
        elif action == "polish":
            kwargs["tone"] = request.style
        elif action == "translate":
            kwargs["target_lang"] = request.target_lang

        async for chunk in service.stream_action(action, **kwargs):
            yield f"data: {json.dumps({'content': chunk})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
