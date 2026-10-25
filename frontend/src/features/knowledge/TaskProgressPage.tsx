/** 任务进度页（/knowledge/tasks/:taskId， §5）。
 *
 * 两级进度：任务级状态 + 文件级 7 态（含阶段内进度与产物统计）；
 * 失败文件按阶段重试（不清空已完成产物）；取消仅排队/解析/切块可用。
 * 轮询 1s（任务进行中），aria-live 节流通知。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { cancelTask, fetchTask, retryTask } from "../../lib/api-client";
import type { TaskFile, TaskView } from "../../types";

const STAGE_LABEL: Record<string, string> = {
  pending: "待上传",
  uploading: "上传中",
  queued: "排队中",
  parsing: "解析中",
  chunking: "切块中",
  indexing: "索引中",
  done: "已完成",
  failed: "失败",
};

const TASK_LABEL: Record<string, string> = {
  queued: "排队中",
  running: "处理中",
  done: "全部完成",
  partial_failed: "部分失败",
  failed: "全部失败",
  cancelled: "已取消",
};

const CANCELLABLE = new Set(["queued", "parsing", "chunking"]);

function FileRow({ file, onRetry }: { file: TaskFile; onRetry: (fileId: string) => void }) {
  const isFailed = file.status === "failed";
  const isDone = file.status === "done";
  const inProgress = !isDone && !isFailed;
  return (
    <div className={`task-file-row${isFailed ? " failed" : ""}`}>
      <span className={`task-status-dot ${isDone ? "done" : isFailed ? "failed" : inProgress ? "running" : "queued"}`} aria-hidden />
      <span style={{ minWidth: 200, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={file.filename}>
        {file.filename}
      </span>
      <span className={`status-tag ${isDone ? "indexed" : isFailed ? "failed" : "parsing"}`}>
        {STAGE_LABEL[file.status] ?? file.status}
        {inProgress && file.stage_progress > 0 && file.stage_progress < 1 && ` ${Math.round(file.stage_progress * 100)}%`}
      </span>
      {isFailed && (
        <span className="trace-id" style={{ color: "var(--error)" }} title={file.error_reason}>
          失败于「{STAGE_LABEL[file.error_stage] ?? file.error_stage}」：{file.error_reason}
        </span>
      )}
      {isDone && (
        <span className="trace-id">
          {file.stats?.chunks ?? "-"} 块 · {(file.stage_elapsed_ms / 1000).toFixed(1)}s
          {file.doc_id && (
            <>
              {" · "}
              <Link to={`/knowledge/preview/${encodeURIComponent(file.doc_id)}`}>查看预览</Link>
            </>
          )}
        </span>
      )}
      {file.stats?.warnings?.map((w, i) => (
        <span key={i} className="trace-id" style={{ color: "var(--warning)" }} title={w}>⚠ {w}</span>
      ))}
      <span style={{ flex: 1 }} />
      {isFailed && (
        <button type="button" className="btn-text" onClick={() => onRetry(file.file_id)}>
          重试
        </button>
      )}
    </div>
  );
}

export function TaskProgressPage() {
  const { taskId = "" } = useParams();
  const [view, setView] = useState<TaskView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const load = useCallback(async () => {
    try {
      const envelope = await fetchTask(taskId);
      if (envelope.data) {
        setView(envelope.data);
        return envelope.data.task.status;
      }
      setError(envelope.error?.message ?? "加载失败");
    } catch {
      setError("网络异常");
    }
    return null;
  }, [taskId]);

  useEffect(() => {
    void load().then((status) => {
      if (status === "queued" || status === "running") {
        timerRef.current = setInterval(() => void load(), 1000);
      }
    });
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [load]);

  // 任务到达终态后停止轮询
  useEffect(() => {
    if (view && !["queued", "running"].includes(view.task.status) && timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, [view]);

  const handleRetry = async (fileId?: string) => {
    const envelope = await retryTask(taskId, fileId);
    if (envelope.data) {
      setNotice(
        `已重试 ${envelope.data.retried_files.length} 个文件（不清空已完成产物${
          envelope.data.reused_parse_products.length ? "，复用解析产物不重复 OCR" : ""
        }）`,
      );
      if (!timerRef.current) timerRef.current = setInterval(() => void load(), 1000);
      void load();
    }
  };

  const handleCancel = async () => {
    if (!window.confirm("确认取消该任务？\n排队中的文件将直接移出；执行中的文件完成当前最小单元后停止；索引中的文件不可取消。")) return;
    const envelope = await cancelTask(taskId);
    if (envelope.data) setNotice(envelope.data.cancelled_scope);
    void load();
  };

  if (error) {
    return (
      <div className="page"><div className="page-inner">
        <div className="error-inline" role="alert"><span aria-hidden>⚠</span>{error}</div>
      </div></div>
    );
  }
  if (!view) {
    return <div className="page"><div className="page-inner"><div className="skeleton" style={{ height: 200 }} /></div></div>;
  }

  const { task, stats } = view;
  const running = ["queued", "running"].includes(task.status);
  const cancellable = running && task.files.some((f) => CANCELLABLE.has(f.status));

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 860 }}>
        <div className="card" aria-live="polite">
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <h3 style={{ margin: 0, flex: 1 }}>
              任务 <span className="mono">{task.task_id}</span>
            </h3>
            <span className={`status-tag ${task.status === "done" ? "indexed" : task.status.includes("failed") ? "failed" : "parsing"}`}>
              {TASK_LABEL[task.status] ?? task.status}
            </span>
            {cancellable && (
              <button type="button" className="btn btn-danger" onClick={() => void handleCancel()}>
                取消任务
              </button>
            )}
          </div>
          <div className="trace-id" style={{ marginTop: 8 }}>
            文件 {stats.done_files}/{task.total_files} 完成
            {stats.failed_files > 0 && ` · ${stats.failed_files} 失败`}
            {running && stats.estimated_remaining_ms > 0 && ` · 预计剩余 ${Math.ceil(stats.estimated_remaining_ms / 1000)}s`}
            {task.chunk_policy && ` · 切块策略 ${(task.chunk_policy as { strategy?: string }).strategy ?? "recursive"}`}
          </div>
          <div className="progress" style={{ marginTop: 8 }} role="progressbar" aria-valuenow={stats.done_files} aria-valuemax={task.total_files}>
            <div style={{ width: `${task.total_files ? (stats.done_files / task.total_files) * 100 : 0}%` }} />
          </div>
          {notice && <div className="pending-note" style={{ marginTop: 8 }}>ℹ {notice}</div>}
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>文件进度</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {task.files.map((f) => (
              <FileRow key={f.file_id} file={f} onRetry={(id) => void handleRetry(id)} />
            ))}
          </div>
          {stats.failed_files > 0 && (
            <div style={{ marginTop: 12 }}>
              <button type="button" className="btn" onClick={() => void handleRetry()}>
                重试全部失败文件
              </button>
            </div>
          )}
          {task.status === "done" && (
            <div className="pending-note" style={{ marginTop: 12 }}>
              ✓ 全部文件已入库。建议到 <Link to="/">对话页</Link> 用一条样例问题验证检索是否命中（入库成功不等于能被检索到）。
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
