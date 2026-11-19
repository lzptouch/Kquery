"""入库任务队列。

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
import threading
import time
import uuid
from typing import Callable, Protocol
from datetime import datetime, timezone
from pathlib import Path

from app.core.tasks import BackgroundQueue
from app.core.envelope import ApiError, ErrCode
from app.services.external_state import ExternalDocStore
from app.domain.schemas import (
    CANCELLABLE_STAGES,
    FILE_CHUNKING,
    FILE_DONE,
    FILE_FAILED,
    FILE_INDEXING,
    FILE_PARSING,
    FILE_QUEUED,
    FILE_UPLOADING,
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
from app.core.logging_setup import get_logger

logger = get_logger(__name__)


class _DocRecordLike(Protocol):
    """后置钩子只用到 doc_id 与 tenant_id，避免钩子与具体记录类型耦合。"""

    doc_id: str
    tenant_id: str


def _task_score(task: IngestTask) -> float:
    """外部存储排序分：`IngestTask` 只有 ISO 字符串的 created_at（无 updated_at 数值字段）。"""
    try:
        return datetime.fromisoformat(task.created_at).timestamp()
    except (ValueError, TypeError):
        return 0.0


#: 后置钩子签名：(文档记录, 解析产物) → 统计字典
PostIndexHook = Callable[["_DocRecordLike", ParseResult], dict]


class TaskManager:
    def __init__(
        self,
        knowledge: KnowledgeService,
        tasks_file: Path,
        ingest_dir: Path,
        post_index: PostIndexHook | None = None,
        workers: int = 2,
        docs: "ExternalDocStore | None" = None,
        retention_s: int = 1800,
    ) -> None:
        self._knowledge = knowledge
        self._tasks_file = tasks_file
        # 任务状态外置（批次 4）：多副本时任务列表/进度要一致
        self._docs = docs
        self._ingest_dir = ingest_dir  # 文件路径按 data/ingest/<task_id>/<filename> 约定推导
        self._lock = threading.Lock()
        self._tasks: dict[str, IngestTask] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        # `A-06`：终态任务在内存里保留一段时间（默认 30 分钟）供前端查看，
        # 之后淘汰。此前**只增不减**——连续上传 600 个文档后任务表就一直涨。
        self._retention_s = max(0, int(retention_s))
        self._terminal_at: dict[str, float] = {}
        self._parse_cache: dict[str, ParseResult] = {}  # file_id → 解析产物（重试复用）
        # 入库后置钩子：图谱构建 + 图像索引（）。不传则退化为纯手动触发。
        self._post_index = post_index
        # 并发提容（批次 3）：有界队列 + 背压。此前是无界队列——批量提交时任务
        # 无限堆积且无法拒绝；满时 submit 返回 False，API 层映射 503。
        #
        # `A-21.1`：队列实现统一到 `core/tasks.BackgroundQueue`
        # ——此前这里是**内联复制**的同一套有界队列逻辑，而 `BackgroundQueue`
        # 自己在生产路径上零调用点（两份实现 = 两份要维护的语义）。
        # 换过去还顺带补上一件一直缺的事：**worker 继承提交方的上下文**
        # （trace_id 等 contextvars），否则入库失败日志与「用户提交了入库」
        # 的日志串不起来（BackgroundQueue 的 `_Job.ctx` 就是干这个的）。
        self._bg = BackgroundQueue(name="ingest", workers=max(1, int(workers)), queue_size=200)
        self._stopping = threading.Event()
        self._load()
        # `A-06`：终态任务淘汰的周期任务（每分钟一次）。
        # 放在这里而不是 lifespan：任务表的生命周期与 TaskManager 相同，
        # 放外面反而要再传一遍停止信号。
        self._evict_thread = threading.Thread(
            target=self._evict_loop, name="ingest-evict", daemon=True
        )
        self._evict_thread.start()

    def task_dir(self, task_id: str) -> Path:
        return self._ingest_dir / task_id

    def file_path(self, task_id: str, filename: str) -> Path:
        return self.task_dir(task_id) / filename

    # ---- 持久化 ----

    _TASK_INDEX = "tasks"

    def _external(self) -> bool:
        return self._docs is not None

    def _load_external(self) -> None:
        assert self._docs is not None
        for doc in self._docs.list_docs(self._TASK_INDEX, limit=500):
            try:
                task = IngestTask.model_validate(doc)
            except Exception:  # noqa: BLE001 - 入库任务解析失败：跳过该条
                logger.debug("入库任务解析失败：跳过该条")
                continue
            # 服务重启时仍在执行中的任务：标记失败（诚实失败，不留假进度）
            interrupted = False
            for f in task.files:
                if f.status in (FILE_PARSING, FILE_CHUNKING, FILE_INDEXING, FILE_QUEUED) \
                        and task.status == TASK_RUNNING:
                    f.status = FILE_FAILED
                    f.error_stage = f.stage
                    f.error_reason = "服务重启导致任务中断，可重试"
                    interrupted = True
            if interrupted:
                task.status = TASK_PARTIAL_FAILED
            self._tasks[task.task_id] = task

    def _load(self) -> None:
        if self._external():
            self._load_external()
            return
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
        if self._external():
            assert self._docs is not None
            for task in self._tasks.values():
                self._docs.put(
                    task.task_id,
                    task.model_dump(),
                    index=self._TASK_INDEX,
                    score=_task_score(task),
                )
            return
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
        tenant_id: str | None = None,
    ) -> IngestTask:
        """创建任务（两阶段提交）：先建档，API 保存完文件后调 submit() 入队。"""
        task = IngestTask(
            task_id=f"t-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
            parse_policy=parse_policy,
            chunk_policy=chunk_policy,
            total_files=len(files),
            created_by=created_by,
            tenant_id=tenant_id or self._knowledge.default_tenant_id,
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

    def submit(self, task_id: str) -> bool:
        """文件落盘后提交任务进入执行队列。队列满返回 False（调用方应拒绝并清理）。"""
        self.get(task_id)
        accepted = self._bg.submit(task_id, lambda: self._run_task(task_id))
        if not accepted:
            logger.warning("入库队列已满（200），任务 %s 被拒", task_id)
        return accepted

    def _evict_loop(self) -> None:
        """周期淘汰终态任务（`A-06`）。"""
        while not self._stopping.is_set():
            # 每分钟一次；保留期由 `retention_s` 控制（默认 30 分钟）
            if self._stopping.wait(timeout=60.0):
                return
            try:
                self.evict_terminal_tasks()
            except Exception:  # noqa: BLE001 - 淘汰失败不该杀死线程
                logger.exception("入库任务淘汰失败（忽略）")

    def evict_terminal_tasks(self, now: float | None = None) -> int:
        """淘汰超过保留期的终态任务（`A-06`），返回淘汰条数。

        **只淘汰终态任务**：排队中 / 执行中的任务永远不淘汰——
        淘汰掉等于把用户的任务凭空消失（前端轮询会拿到 404，且无法重试）。

        保留期默认 30 分钟：够前端看完进度，也够运维复盘。
        """
        if self._retention_s <= 0:
            return 0
        ts = time.time() if now is None else now
        terminal = {TASK_DONE, TASK_FAILED, TASK_PARTIAL_FAILED, TASK_CANCELLED}
        evicted = 0
        with self._lock:
            for task_id, task in list(self._tasks.items()):
                if task.status not in terminal:
                    continue
                finished = self._terminal_at.get(task_id)
                if finished is None:
                    # 终态但没有时间戳（进程重启后从磁盘恢复的）：按「现在」算，
                    # 避免刚启动就把历史任务全清了。
                    self._terminal_at[task_id] = ts
                    continue
                if ts - finished < self._retention_s:
                    continue
                self._tasks.pop(task_id, None)
                self._cancel_flags.pop(task_id, None)
                self._terminal_at.pop(task_id, None)
                evicted += 1
        if evicted:
            logger.info("入库任务淘汰：清掉 %d 个超过 %ds 的终态任务", evicted, self._retention_s)
        return evicted

    def discard(self, task_id: str) -> None:
        """废弃尚未入队的任务（上传校验失败时回收任务目录与记录）。"""
        import shutil

        with self._lock:
            self._tasks.pop(task_id, None)
            self._cancel_flags.pop(task_id, None)
            self._terminal_at.pop(task_id, None)
            self._save()
        shutil.rmtree(self.task_dir(task_id), ignore_errors=True)

    def remove_files_for_doc(self, doc_id: str) -> int:
        """删除文档时联动清理入库任务的落盘原件（对照 KQ-08：删除必须收敛派生副本）。

        只删物理文件，不改任务历史（任务是运维事实记录，保留可追溯）。
        """
        removed = 0
        for task in list(self._tasks.values()):
            for f in task.files:
                if f.doc_id != doc_id:
                    continue
                try:
                    path = self.file_path(task.task_id, f.filename)
                    if path.exists():
                        path.unlink()
                        removed += 1
                except OSError as exc:
                    logger.warning("任务原件清理失败 %s/%s: %s", task.task_id, f.filename, exc)
        return removed

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

    def shutdown(self, timeout_s: float = 30.0) -> None:
        """优雅停机（批次 3）：等在飞任务完成当前文件后退出，不再接受新任务。

        此前 daemon 线程被进程退出硬杀，可能留下半截索引与半截 tasks.json。
        """
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._bg.shutdown(wait=True, timeout=timeout_s)

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
            # 终态时间戳（`A-06`）：从现在起保留一段时间再淘汰
            self._terminal_at[task_id] = time.time()
            self._save()
            logger.info("任务结束: %s → %s", task_id, task.status)

    def _progress_reporter(self, file: FileRecord):
        """返回一个 progress(done, total) 回调，供解析器/索引器上报真实进度。

        没有它，扫描件多页 OCR 时进度条会从 10% 直接跳到 100%——
        用户看到的就是"卡住了十几分钟然后突然好了"。
        """
        def report(done: int, total: int, stage: str | None = None) -> None:
            if total <= 0:
                return
            ratio = max(0.0, min(1.0, done / total))
            with self._lock:
                if stage:
                    file.stage = stage
                    file.status = stage
                file.stage_progress = round(ratio, 4)
                file.stage_elapsed_ms = int((time.perf_counter() - self._stage_started(file)) * 1000)
                self._save()
        return report

    @staticmethod
    def _stage_started(file: FileRecord) -> float:
        started = getattr(file, "_stage_started_at", None)
        if started is None:
            started = time.perf_counter()
            object.__setattr__(file, "_stage_started_at", started)
        return started

    @staticmethod
    def _quality_flags(record, parsed, short_chunks: int, pages: int) -> list[str]:
        """入库质量红线（）：不阻断入库，只如实标注供调参。

        质量红线只标记不阻断——阻断会让"参数不合适"直接变成"文件进不来"，
        运营反而看不到问题规模。
        """
        flags: list[str] = []
        ratio = short_chunks / max(record.chunk_count, 1)
        if ratio > 0.10:
            flags.append(f"超短块占比 {ratio:.1%} > 10%，建议调整 chunk_size")
        if record.chunk_count == 0:
            flags.append("未产出任何块（文档无可抽取文本）")
        if parsed.segments and pages / max(record.chunk_count, 1) < 0.2 and record.chunk_count < 5:
            flags.append("块数明显少于页数，可能存在整段漏切")
        if len(parsed.warnings) > 3:
            flags.append(f"解析告警 {len(parsed.warnings)} 条，建议人工抽检")
        return flags

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
            # 建档（幂等：重试时复用已有 doc 记录）；租户取任务提交主体（KQ-02）
            content = path.read_bytes()
            record = self._knowledge.create_record(
                filename, content, source_path=path, tenant_id=task.tenant_id,
            )
            file.doc_id = record.doc_id
            # 上传落盘 → 解析之前也要占一个可见态（否则"上传中"两态在前端永远不出现）
            set_stage(FILE_UPLOADING, 1.0)

            # ① 解析（OCR 可能是分钟级，阶段内进度必需）
            parsed = self._parse_cache.get(file.file_id)
            if parsed is None:
                set_stage(FILE_PARSING, 0.05)
                parsed = self._knowledge.parse_document(
                    record, task.parse_policy, progress=self._progress_reporter(file)
                )
                self._parse_cache[file.file_id] = parsed
            set_stage(FILE_PARSING, 1.0)
            if cancel_flag.is_set():
                raise _Cancelled()

            # ② 切块 + ③ 索引（临界区：进入后不可取消）
            set_stage(FILE_CHUNKING, 0.1)
            self._knowledge.apply_chunking(record, parsed, task.chunk_policy)
            set_stage(FILE_CHUNKING, 1.0)
            set_stage(FILE_INDEXING, 0.1)
            # 索引阶段内部按"已处理块 / 总块数"推进，嵌入调用是最耗时的一段
            self._knowledge.index_parsed(
                record, parsed, task.chunk_policy,
                progress=self._progress_reporter(file),
            )
            set_stage(FILE_INDEXING, 1.0)

            # ④ 图谱 / 图像索引联动（：入库后触发，而非纯手动）。
            # 刻意放在"索引成功之后、DONE 之前"：图谱与图像都是增强层，
            # 它们的失败不能把一份已经可检索的文档打成入库失败。
            if self._post_index is not None:
                try:
                    post_stats = self._post_index(record, parsed)
                except Exception as exc:  # noqa: BLE001 - 图谱/图像索引联动失败
                    logger.warning("图谱/图像索引联动失败（不影响入库）: %s", exc)
                    post_stats = {"error": str(exc)[:200]}
                else:
                    if post_stats:
                        logger.info(
                            "图谱/图像索引联动完成 %s: %s", record.doc_id, post_stats
                        )
                        file.graph_stats = post_stats

            file.status = FILE_DONE
            file.stage = FILE_DONE
            file.stage_progress = 1.0
            file.stage_elapsed_ms = int((time.perf_counter() - stage_started) * 1000)
            # 产物统计补齐文档 §6.2 质量红线所需的两项：页数与超短块数。
            # 缺了它们"超短块占比 >10% 需调参"与"页数异常"这两条红线无数据可判。
            short_chunks = int(getattr(record, "short_chunk_count", 0) or 0)
            pages = max(1, len(parsed.segments))
            file.stats = {
                "chunks": record.chunk_count,
                "tables": len(parsed.tables),
                "assets": len(parsed.assets),
                "warnings": parsed.warnings,
                "segments": len(parsed.segments),
                "pages": pages,
                "short_chunks": short_chunks,
                "short_chunk_ratio": round(short_chunks / max(record.chunk_count, 1), 4),
                "quality_flags": self._quality_flags(record, parsed, short_chunks, pages),
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
        # 重试走同一条队列（背压语义一致：满时返回 False，调用方据此提示稍后重试）
        self._bg.submit(task_id, lambda: self._run_task(task_id))
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
