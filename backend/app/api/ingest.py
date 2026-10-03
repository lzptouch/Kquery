"""入库任务端点（阶段 3 前端规范 §7）。

- POST /knowledge/ingest：提交入库任务（多文件 + 解析策略 + 切块策略），立即返回 task_id；
- GET  /knowledge/tasks/{id}：任务进度（文件级 7 态 + 阶段耗时 + 产物统计 + 预估剩余）；
- POST /knowledge/tasks/{id}/retry：重试（file_id 可选，缺省整批失败文件；不清空已完成产物）；
- POST /knowledge/tasks/{id}/cancel：取消（仅排队/解析/切块可取消，索引中不可取消）。
"""
import json
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.services.chunkers.factory import validate_chunk_policy
from app.services.parsers.router import SUPPORTED_EXTENSIONS

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["ingest"])

MAX_BATCH_FILES = 20


def _parse_policy_field(raw: str | None, name: str) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        return value
    except ValueError:
        raise ApiError(ErrCode.INVALID_INPUT, f"{name} 必须是 JSON 对象") from None


@router.post("/ingest", response_model=Envelope)
async def ingest(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()

    form = await request.form()
    parse_policy = _parse_policy_field(form.get("parse_policy"), "parse_policy")
    chunk_policy = _parse_policy_field(form.get("chunk_policy"), "chunk_policy")
    if error := validate_chunk_policy(chunk_policy):
        raise ApiError(ErrCode.CHUNK_POLICY_INVALID, error)

    # 多文件同名字段必须用 getlist（form.values() 对同名键只返回一个）
    uploads = [v for v in form.getlist("files") if isinstance(v, StarletteUploadFile)]
    if not uploads:
        raise ApiError(ErrCode.INVALID_INPUT, "未包含任何文件")
    if len(uploads) > MAX_BATCH_FILES:
        raise ApiError(ErrCode.INVALID_INPUT, f"单次最多 {MAX_BATCH_FILES} 个文件")

    settings = request.app.state.settings
    manager = request.app.state.task_manager

    # 上传前校验（兜底，前端已拦截）：扩展名与大小
    files_meta: list[dict] = []
    contents: list[tuple[str, bytes]] = []
    for upload in uploads:
        filename = upload.filename or "unnamed"
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in SUPPORTED_EXTENSIONS:
            raise ApiError(ErrCode.UNSUPPORTED_FILE, f"不支持的文件类型 {ext}: {filename}")
        content = await upload.read()
        if len(content) > settings.max_upload_mb * 1024 * 1024:
            raise ApiError(ErrCode.FILE_TOO_LARGE, f"{filename} 超过 {settings.max_upload_mb}MB 限制")
        files_meta.append({"filename": filename, "size": len(content)})
        contents.append((filename, content))

    # 两阶段提交：先建任务 → 文件落盘 → 入队执行
    task = manager.create_task(files_meta, parse_policy, chunk_policy)
    task_dir = manager.task_dir(task.task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    for filename, content in contents:
        (task_dir / filename).write_bytes(content)
    manager.submit(task.task_id)

    return ok_envelope(
        {"task_id": task.task_id, "total_files": task.total_files},
        trace_id,
        elapsed_ms_since(started),
    )


@router.get("/tasks/{task_id}", response_model=Envelope)
def get_task(task_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    view = request.app.state.task_manager.view(task_id)
    return ok_envelope(view, trace_id, elapsed_ms_since(started))


class RetryRequest(BaseModel):
    file_id: str | None = None


@router.post("/tasks/{task_id}/retry", response_model=Envelope)
def retry_task(task_id: str, req: RetryRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    result = request.app.state.task_manager.retry(task_id, req.file_id)
    return ok_envelope(result, trace_id, elapsed_ms_since(started))


@router.post("/tasks/{task_id}/cancel", response_model=Envelope)
def cancel_task(task_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    result = request.app.state.task_manager.cancel(task_id)
    return ok_envelope(result, trace_id, elapsed_ms_since(started))
