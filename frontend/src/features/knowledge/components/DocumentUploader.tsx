/** 文档上传器：拖拽 / 点击选择 / 批量上传，
 * 扩展名与大小前置校验（不通过的文件标红并说明原因，其余继续），
 * 失败可重试。支持选择目标知识库（缺省归默认库）。
 */
import { useEffect, useRef, useState } from "react";
import { fetchKnowledgeBases, uploadFile } from "../../../lib/api-client";
import { ACCEPT_ATTRIBUTE, validateExtension } from "../../../lib/file-types";
import type { KnowledgeBase } from "../../../types";

interface Props {
  accept?: string;
  maxSizeMB?: number;
  onUploaded: () => void;
}

interface QueueItem {
  id: string;
  file: File;
  status: "pending" | "uploading" | "success" | "error" | "invalid";
  progress: number;
  error?: string;
}

export function DocumentUploader({ maxSizeMB = 50, onUploaded }: Props) {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [dragover, setDragover] = useState(false);
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [targetKb, setTargetKb] = useState("default");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // 拉取失败不阻断上传：退回默认库即可（后端缺省同样归 default）
    // 必须显式 catch：没有 catch 的 Promise 拒绝会变成 Unhandled Rejection——
    // 浏览器控制台报 "Uncaught (in promise)"，E2E 的 console error 断言也会被它打挂。
    void fetchKnowledgeBases()
      .then((env) => {
        if (env.data) setKbs(env.data);
      })
      .catch(() => {
        // 列表拿不到就用默认库，不打扰用户
      });
  }, []);

  const updateItem = (id: string, patch: Partial<QueueItem>) => {
    setQueue((prev) => prev.map((it) => (it.id === id ? { ...it, ...patch } : it)));
  };

  const validate = (file: File): string | null => {
    const extError = validateExtension(file.name);
    if (extError) return extError;
    if (file.size > maxSizeMB * 1024 * 1024) return `超过 ${maxSizeMB}MB 限制`;
    return null;
  };

  const doUpload = async (item: QueueItem) => {
    updateItem(item.id, { status: "uploading", progress: 0, error: undefined });
    try {
      const envelope = await uploadFile(item.file, (p) => updateItem(item.id, { progress: p }), targetKb);
      if (envelope.status === "error" || !envelope.data) {
        updateItem(item.id, {
          status: "error",
          error: envelope.error?.message ?? "上传失败",
        });
        return;
      }
      if (envelope.data.status === "failed") {
        updateItem(item.id, {
          status: "error",
          error: envelope.data.error ?? "解析失败，请检查文件是否加密或损坏",
        });
      } else {
        updateItem(item.id, { status: "success", progress: 100 });
      }
      onUploaded();
    } catch (err) {
      updateItem(item.id, { status: "error", error: (err as Error).message });
    }
  };

  const addFiles = (files: FileList | File[]) => {
    const items: QueueItem[] = Array.from(files).map((file) => {
      const invalid = validate(file);
      return {
        id: `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
        file,
        status: invalid ? "invalid" : "pending",
        progress: 0,
        error: invalid ?? undefined,
      } as QueueItem;
    });
    setQueue((prev) => [...prev, ...items]);
    items.filter((it) => it.status === "pending").forEach((it) => void doUpload(it));
  };

  const removeItem = (id: string) => setQueue((prev) => prev.filter((it) => it.id !== id));

  return (
    <div>
      {kbs.length > 1 && (
        <label className="graph-filter" style={{ marginBottom: 8, display: "inline-flex", gap: 6 }}>
          上传到知识库
          <select
            className="probe-input"
            aria-label="目标知识库"
            value={targetKb}
            onChange={(e) => setTargetKb(e.target.value)}
          >
            {kbs.map((k) => (
              <option key={k.kb_id} value={k.kb_id}>
                {k.name}{k.is_public ? "（公开）" : ""}
              </option>
            ))}
          </select>
        </label>
      )}
      <div
        className={`uploader-zone${dragover ? " dragover" : ""}`}
        role="button"
        tabIndex={0}
        aria-label="上传文档：点击选择或拖入文件"
        onClick={() => inputRef.current?.click()}
        onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragover(true);
        }}
        onDragLeave={() => setDragover(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragover(false);
          addFiles(e.dataTransfer.files);
        }}
      >
        <div style={{ fontSize: 28 }} aria-hidden>
          ⬆
        </div>
        <div>拖拽文件到此处，或点击选择（支持 PDF / Markdown / Word / Excel / PPT / 纯文本 / 代码 / 图片 / 音频，单文件 ≤ {maxSizeMB}MB）</div>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT_ATTRIBUTE}
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files) addFiles(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {queue.length > 0 && (
        <div className="upload-queue">
          {queue.map((item) => (
            <div key={item.id} className={`upload-item${item.status === "invalid" ? " invalid" : ""}`}>
              <span style={{ minWidth: 180, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {item.file.name}
              </span>
              <span className="trace-id">{(item.file.size / 1024).toFixed(0)}KB</span>
              {item.status === "uploading" && (
                <>
                  <div className="progress" role="progressbar" aria-valuenow={item.progress}>
                    <div style={{ width: `${item.progress}%` }} />
                  </div>
                  <span>{item.progress}%</span>
                </>
              )}
              {item.status === "success" && <span style={{ color: "var(--success)" }}>✓ 已索引</span>}
              {(item.status === "error" || item.status === "invalid") && (
                <>
                  <span style={{ color: "var(--error)", flex: 1 }}>{item.error}</span>
                  {item.status === "error" && (
                    <button type="button" className="btn-text" onClick={() => void doUpload(item)}>
                      重试
                    </button>
                  )}
                </>
              )}
              <button type="button" className="btn-text" aria-label="移除" onClick={() => removeItem(item.id)}>
                ✕
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
