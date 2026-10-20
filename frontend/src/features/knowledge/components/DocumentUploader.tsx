/** 文档上传器：拖拽 / 点击选择 / 批量上传，
 * 扩展名与大小前置校验（不通过的文件标红并说明原因，其余继续），
 * 失败可重试。
 */
import { useRef, useState } from "react";
import { uploadFile } from "../../../lib/api-client";

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

const ALLOWED_EXT = [".pdf", ".md", ".markdown", ".docx", ".png", ".jpg", ".jpeg", ".mp3", ".wav"];

export function DocumentUploader({ maxSizeMB = 50, onUploaded }: Props) {
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [dragover, setDragover] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const updateItem = (id: string, patch: Partial<QueueItem>) => {
    setQueue((prev) => prev.map((it) => (it.id === id ? { ...it, ...patch } : it)));
  };

  const validate = (file: File): string | null => {
    const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
    if (!ALLOWED_EXT.includes(ext)) return `不支持的格式 ${ext}（支持 PDF / Markdown / Word / 图片 / 音频）`;
    if (file.size > maxSizeMB * 1024 * 1024) return `超过 ${maxSizeMB}MB 限制`;
    return null;
  };

  const doUpload = async (item: QueueItem) => {
    updateItem(item.id, { status: "uploading", progress: 0, error: undefined });
    try {
      const envelope = await uploadFile(item.file, (p) => updateItem(item.id, { progress: p }));
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
        <div>拖拽文件到此处，或点击选择（支持 PDF / Markdown / Word / 图片 / 音频，单文件 ≤ {maxSizeMB}MB）</div>
        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.md,.markdown,.docx,.png,.jpg,.jpeg,.mp3,.wav"
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
