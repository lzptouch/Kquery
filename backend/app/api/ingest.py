"""入库任务端点。

- POST /knowledge/ingest：提交入库任务（多文件 + 解析策略 + 切块策略），立即返回 task_id；
- GET  /knowledge/tasks/{id}：任务进度（文件级 7 态 + 阶段耗时 + 产物统计 + 预估剩余）；
- POST /knowledge/tasks/{id}/retry：重试（file_id 可选，缺省整批失败文件；不清空已完成产物）；
- POST /knowledge/tasks/{id}/cancel：取消（仅排队/解析/切块可取消，索引中不可取消）。
"""
import json
import logging
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.api.upload_util import sanitize_filename, stream_to_disk
from app.api.kb import resolve_kb_for_write
from app.core.envelope import ApiError, Envelope, ErrCode, ok_envelope
from app.dependencies import enforce_rate_limit, require_role
from app.core.tracing import elapsed_ms_since, new_trace_id, now_ms, set_trace_id
from app.services.chunkers.factory import validate_chunk_policy
from app.services.parsers.router import SUPPORTED_EXTENSIONS, validate_parse_policy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge", tags=["ingest"], dependencies=[Depends(require_role("admin"))])

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


@router.post("/ingest", response_model=Envelope, dependencies=[Depends(enforce_rate_limit)])
async def ingest(request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()

    form = await request.form()
    parse_policy = _parse_policy_field(form.get("parse_policy"), "parse_policy")
    chunk_policy = _parse_policy_field(form.get("chunk_policy"), "chunk_policy")
    # 解析策略的字段类型必须校验：策略全是开关，类型错了会被真值语义悄悄接受
    # （`{"pdf_tables": "yes"}` → 真值 → 表格识别照开），用户以为关了其实没关。
    if error := validate_parse_policy(parse_policy):
        raise ApiError(ErrCode.INVALID_INPUT, error)
    kb_id_raw = form.get("kb_id")
    kb_id = str(kb_id_raw).strip() if kb_id_raw else "default"
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
    principal = getattr(request.state, "principal", None)
    tenant_id = principal.tenant_id if principal else settings.default_tenant_id
    kb_id = resolve_kb_for_write(request, kb_id, tenant_id)

    # 上传前校验（兜底，前端已拦截）：文件名收敛（KQ-01）、同批重名拒绝、
    # 扩展名、大小（边写边计数，KQ-05）+ PII 硬拦截
    staging = settings.data_dir / "ingest" / f".staging-{uuid.uuid4().hex[:12]}"
    staging.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    files_meta: list[dict] = []
    try:
        for upload in uploads:
            filename = sanitize_filename(upload.filename or "")
            ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if ext not in SUPPORTED_EXTENSIONS:
                raise ApiError(ErrCode.UNSUPPORTED_FILE, f"不支持的文件类型 {ext}: {filename}")
            if filename in seen:
                raise ApiError(
                    ErrCode.INVALID_INPUT,
                    f"同一批次存在同名文件 {filename}，请重命名后再上传",
                    http_status=400,
                )
            seen.add(filename)
            size = await stream_to_disk(upload, staging / filename, settings.max_upload_mb)
            files_meta.append({"filename": filename, "size": size})
    except BaseException:
        import shutil

        shutil.rmtree(staging, ignore_errors=True)
        raise

    # 两阶段提交：先建任务 → 文件移入任务目录 → 入队执行
    task = manager.create_task(files_meta, parse_policy, chunk_policy, tenant_id=tenant_id,
                               kb_id=kb_id)
    task_dir = manager.task_dir(task.task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    for meta in files_meta:
        (staging / meta["filename"]).replace(task_dir / meta["filename"])
    staging.rmdir()

    # 说明：入库侧的内容审查（敏感词 / PII 硬拦截）已随敏感词功能下线，
    # 文档一旦入库即可被检索引用并进入回答，此处不再做入口拦截。
    if not manager.submit(task.task_id):
        # 队列背压：拒绝时回收任务与已落盘文件，不留下"已受理但永不执行"的任务
        manager.discard(task.task_id)
        raise ApiError(
            ErrCode.RATE_LIMITED, "入库队列已满，请稍后重试",
            http_status=503, retry_after=30,
        )
    return ok_envelope(
        {"task_id": task.task_id, "total_files": task.total_files},
        trace_id,
        elapsed_ms_since(started),
    )


def _assert_task_tenant(request: Request, task_id: str) -> None:
    """任务归属断言（KQ-02）：任务详情/重试/取消只对提交租户可见。"""
    task = request.app.state.task_manager.get(task_id)
    principal = getattr(request.state, "principal", None)
    if principal is not None and task.tenant_id != principal.tenant_id:
        raise ApiError(ErrCode.TASK_NOT_FOUND, f"任务不存在: {task_id}", http_status=404)


@router.get("/tasks/{task_id}", response_model=Envelope)
def get_task(task_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _assert_task_tenant(request, task_id)
    view = request.app.state.task_manager.view(task_id)
    return ok_envelope(view, trace_id, elapsed_ms_since(started))


class RetryRequest(BaseModel):
    file_id: str | None = None


@router.post("/tasks/{task_id}/retry", response_model=Envelope)
def retry_task(task_id: str, req: RetryRequest, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _assert_task_tenant(request, task_id)
    result = request.app.state.task_manager.retry(task_id, req.file_id)
    return ok_envelope(result, trace_id, elapsed_ms_since(started))


@router.post("/tasks/{task_id}/cancel", response_model=Envelope)
def cancel_task(task_id: str, request: Request) -> Envelope:
    trace_id = new_trace_id()
    set_trace_id(trace_id)
    started = now_ms()
    _assert_task_tenant(request, task_id)
    result = request.app.state.task_manager.cancel(task_id)
    return ok_envelope(result, trace_id, elapsed_ms_since(started))
