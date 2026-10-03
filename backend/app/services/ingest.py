"""入库任务队列（阶段 3 §4.1 / §6.4 / 前端规范 §3）。

- 入库从"一次请求一次结果"升级为**可查询、可重试、可取消**的长流程：
  用户上传后立即返回 task_id，前端轮询任务进度页；
- 文件级 7 态：排队 → 解析 → 切块 → 索引 → 完成（任一处理态可失败）；
- 失败记录失败阶段（error_stage）：解析/切块失败按单文件重试，
  索引失败属一致性临界区，重试不清空已完成产物；
- 取消语义：仅排队/解析/切块阶段可取消（索引中取消会留下半成品索引）；
- 重试复用解析产物缓存：已解析但切块失败的文件重试直接进入切块，
  不重复执行 OCR（大文件失败重试代价可控的关键）。
"""
import json
import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.envelope import ApiError, ErrCode
from app.domain.schemas import (
    CANCELLABLE_STAGES,
    FILE_CHUNKING,
    FILE_DONE,
    FILE_FAILED,
    FILE_INDEXING,
    FILE_PARSING,
    FILE_QUEUED,
    TASK_CANCELLED,
    TASK_DONE,
    TASK_FAILED,
    TASK_PARTIAL_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    FileRecord,
    IngestTask,
)
from app.services.knowledge import KnowledgeService
from app.services.parsers.base import ParseResult

logger = logging.getLogger(__name__)


class TaskManager:
    def __init__(self, knowledge: KnowledgeService, tasks_file: Path, ingest_dir: Path):
        self._knowledge = knowledge
        self._tasks_file = tasks_file
        self._ingest_dir = ingest_dir  # 文件路径按 data/ingest/<task_id>/<filename> 约定推导
        self._lock = threading.Lock()
        self._tasks: dict[str, IngestTask] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._parse_cache: dict[str, ParseResult] = {}  # file_id → 解析产物（重试复用）
        self._queue: queue.Queue[str] = queue.Queue()
        self._load()
        self._worker = threading.Thread(target=self._loop, name="ingest-worker", daemon=True)
        self._worker.start()

    def task_dir(self, task_id: str) -> Path:
        return self._ingest_dir / task_id

    def file_path(self, task_id: str, filename: str) -> Path:
        return self.task_dir(task_id) / filename

    # ---- 持久化 ----

    def _load(self) -> None:
        if not self._tasks_file.exists():
            return
        try:
            raw = json.loads(self._tasks_file.read_text(encoding="utf-8"))
            for payload in raw:
                task = IngestTask.model_validate(payload)
                # 服务重启时仍在执行中的文件：标记失败（诚实失败，不留假进度）
                interrupted = False
                for f in task.files:
                    if f.status in (FILE_PARSING, FILE_CHUNKING, FILE_INDEXING, FILE_QUEUED) and task.status == TASK_RUNNING:
                        f.status = FILE_FAILED
                        f.error_stage = f.stage
                        f.error_reason = "服务重启导致任务中断，可重试"
                        interrupted = True
                if interrupted:
                    task.status = TASK_PARTIAL_FAILED
                self._tasks[task.task_id] = task
        except Exception as exc:
            logger.warning("任务文件损坏（忽略并重置）: %s", exc)

    def _save(self) -> None:
        self._tasks_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._tasks_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([t.model_dump() for t in self._tasks.values()], ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._tasks_file)

    # ---- 任务创建 ----

    def create_task(
        self,
        files: list[dict],  # [{filename, size}]
        parse_policy: dict,
        chunk_policy: dict,
        created_by: str = "system",
    ) -> IngestTask:
        """创建任务（两阶段提交）：先建档，API 保存完文件后调 submit() 入队。"""
        task = IngestTask(
            task_id=f"t-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
            parse_policy=parse_policy,
            chunk_policy=chunk_policy,
            total_files=len(files),
            created_by=created_by,
            created_at=datetime.now(timezone.utc).isoformat(),
            files=[
                FileRecord(
                    file_id=uuid.uuid4().hex[:12],
                    task_id="",
                    filename=f["filename"],
                    size=f.get("size", 0),
                )
                for f in files
            ],
        )
        for f in task.files:
            f.task_id = task.task_id
        with self._lock:
            self._tasks[task.task_id] = task
            self._cancel_flags[task.task_id] = threading.Event()
            self._save()
        logger.info("入库任务已创建: %s（%d 个文件）", task.task_id, task.total_files)
        return task

    def submit(self, task_id: str) -> None:
        """文件落盘后提交任务进入执行队列。"""
        self.get(task_id)
        self._queue.put(task_id)

    # ---- 查询 ----

    def get(self, task_id: str) -> IngestTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise ApiError(ErrCode.TASK_NOT_FOUND, f"任务不存在: {task_id}", http_status=404)
        return task

    def view(self, task_id: str) -> dict:
        task = self.get(task_id)
        done = [f for f in task.files if f.status == FILE_DONE]
        remaining = [f for f in task.files if f.status not in (FILE_DONE, FILE_FAILED)]
        # 预估剩余：已完成文件的平均耗时 × 剩余文件数
        avg_ms = 0
        if done:
            avg_ms = sum(f.stage_elapsed_ms for f in done) // len(done)
        stats = {
            "estimated_remaining_ms": avg_ms * len(remaining),
            "done_files": len(done),
            "failed_files": len([f for f in task.files if f.status == FILE_FAILED]),
            "remaining_files": len(remaining),
        }
        return {"task": task.model_dump(), "stats": stats}

    # ---- 执行 ----

    def _loop(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                self._run_task(task_id)
            except Exception:
                logger.exception("任务执行异常: %s", task_id)

    def _run_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks[task_id]
            task.status = TASK_RUNNING
            self._save()
        cancel_flag = self._cancel_flags[task_id]

        for file in task.files:
            if file.status == FILE_DONE:
                continue
            # 取消语义：排队中的文件直接移出；执行中完成当前最小单元后停止
            if cancel_flag.is_set() and file.status in CANCELLABLE_STAGES | {FILE_QUEUED}:
                file.status = FILE_FAILED
                file.error_stage = file.stage
                file.error_reason = "已被用户取消"
                continue
            self._process_file(task, file, cancel_flag)
            with self._lock:
                task.done_files = len([f for f in task.files if f.status == FILE_DONE])
                self._save()

        with self._lock:
            statuses = {f.status for f in task.files}
            if cancel_flag.is_set():
                task.status = TASK_CANCELLED
            elif statuses == {FILE_DONE}:
                task.status = TASK_DONE
            elif FILE_DONE in statuses:
                task.status = TASK_PARTIAL_FAILED
            else:
                task.status = TASK_FAILED
            self._save()
            logger.info("任务结束: %s → %s", task_id, task.status)

    def _process_file(self, task: IngestTask, file: FileRecord, cancel_flag: threading.Event) -> None:
        path = self.file_path(task.task_id, file.filename)
        filename = file.filename
        stage_started = time.perf_counter()

        def set_stage(stage: str, progress: float = 0.0) -> None:
            nonlocal stage_started
            file.stage = stage
            file.status = stage
            file.stage_progress = progress
            file.stage_elapsed_ms = int((time.perf_counter() - stage_started) * 1000)
            stage_started = time.perf_counter()
            with self._lock:
                self._save()

        try:
            # 建档（幂等：重试时复用已有 doc 记录）
            content = path.read_bytes()
            record = self._knowledge.create_record(filename, content, source_path=path)
            file.doc_id = record.doc_id

            # ① 解析（OCR / 转写可能是分钟级，阶段内进度必需）
            parsed = self._parse_cache.get(file.file_id)
            if parsed is None:
                set_stage(FILE_PARSING, 0.1)
                parsed = self._knowledge.parse_document(record, task.parse_policy)
                self._parse_cache[file.file_id] = parsed
            set_stage(FILE_PARSING, 1.0)
            if cancel_flag.is_set():
                raise _Cancelled()

            # ② 切块 + ③ 索引（临界区：进入后不可取消）
            set_stage(FILE_CHUNKING, 0.3)
            set_stage(FILE_INDEXING, 0.5)
            self._knowledge.index_parsed(record, parsed, task.chunk_policy)
            set_stage(FILE_INDEXING, 1.0)

            file.status = FILE_DONE
            file.stage = FILE_DONE
            file.stage_progress = 1.0
            file.stage_elapsed_ms = int((time.perf_counter() - stage_started) * 1000)
            file.stats = {
                "chunks": record.chunk_count,
                "tables": len(parsed.tables),
                "assets": len(parsed.assets),
                "warnings": parsed.warnings,
                "segments": len(parsed.segments),
            }
            self._parse_cache.pop(file.file_id, None)
            logger.info("文件入库完成: %s → %s（%d 块）", filename, record.doc_id, record.chunk_count)
        except _Cancelled:
            file.status = FILE_FAILED
            file.error_stage = file.stage
            file.error_reason = "已被用户取消"
        except ApiError as exc:
            file.status = FILE_FAILED
            file.error_stage = file.stage
            file.error_reason = exc.message
            logger.warning("文件入库失败: %s（阶段=%s）: %s", filename, file.stage, exc.message)
        except Exception as exc:  # noqa: BLE001 - 单文件失败不阻塞其它文件
            file.status = FILE_FAILED
            file.error_stage = file.stage
            file.error_reason = f"{type(exc).__name__}: {exc}"
            logger.exception("文件入库异常: %s", filename)
        finally:
            with self._lock:
                self._save()

    # ---- 重试与取消 ----

    def retry(self, task_id: str, file_id: str | None = None) -> dict:
        """重试失败文件。不清空已完成产物；解析产物缓存复用（不重复 OCR）。"""
        task = self.get(task_id)
        targets = [
            f
            for f in task.files
            if f.status == FILE_FAILED and (file_id is None or f.file_id == file_id)
        ]
        if file_id is not None and not targets:
            raise ApiError(ErrCode.INVALID_INPUT, f"文件 {file_id} 不在失败列表中")
        if not targets:
            raise ApiError(ErrCode.INVALID_INPUT, "没有可重试的失败文件")
        with self._lock:
            for f in targets:
                f.status = FILE_QUEUED
                f.stage = FILE_QUEUED
                f.stage_progress = 0.0
                # 解析阶段失败的清除缓存重新解析；切块/索引失败的复用解析产物
                if f.error_stage == FILE_PARSING:
                    self._parse_cache.pop(f.file_id, None)
                f.error_stage = ""
                f.error_reason = ""
            task.status = TASK_QUEUED
            self._save()
        self._queue.put(task_id)
        return {
            "retried_files": [f.file_id for f in targets],
            "cleared_products": False,  # 重试不清空已完成产物
            "reused_parse_products": [
                f.file_id for f in targets if f.file_id in self._parse_cache
            ],
        }

    def cancel(self, task_id: str) -> dict:
        task = self.get(task_id)
        flag = self._cancel_flags.get(task_id)
        if flag is None:
            raise ApiError(ErrCode.TASK_NOT_FOUND, f"任务不存在: {task_id}", http_status=404)
        flag.set()
        running_stages = {f.stage for f in task.files if f.status not in (FILE_DONE, FILE_FAILED)}
        scope = (
            "排队中的文件将直接移出，执行中的文件完成当前最小单元后停止"
            if running_stages & CANCELLABLE_STAGES
            else "当前文件处于索引阶段（不可取消），将等待其完成后终止"
        )
        return {"cancelled_scope": scope}


class _Cancelled(Exception):
    pass
