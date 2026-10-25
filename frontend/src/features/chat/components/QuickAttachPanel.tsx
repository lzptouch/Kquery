/** 快问附件面板（F4）：上传单个文件 → 只针对它提问（不入知识库）。
 *
 * 与"知识库上传"的区别必须让用户一眼看懂：
 * - 文件**不进**知识库，不出现在文件列表，2 小时后自动清理；
 * - 提问时只检索这一个文件，答案的证据全部来自它。
 */
import { useRef, useState } from "react";
import { deleteQuickAsk, uploadQuickAsk } from "../../../lib/api-client";
import { ACCEPT_ATTRIBUTE, validateExtension } from "../../../lib/file-types";

export interface QuickDoc {
  docId: string;
  filename: string;
  chunks: number;
}

interface Props {
  current: QuickDoc | null;
  onAttach: (doc: QuickDoc) => void;
  onDetach: () => void;
  onClose: () => void;
  /** 当前会话（给了就把临时文档绑定到该会话） */
  sessionId?: string | null;
}

export function QuickAttachPanel({ current, onAttach, onDetach, onClose, sessionId }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handleFile = async (file: File) => {
    const extError = validateExtension(file.name);
    if (extError) {
      setError(extError);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const envelope = await uploadQuickAsk(file, sessionId);
      if (!envelope.data) {
        setError(envelope.error?.message ?? "上传失败");
        return;
      }
      onAttach({
        docId: envelope.data.doc_id,
        filename: envelope.data.filename,
        chunks: envelope.data.chunks,
      });
    } catch {
      setError("网络异常，请重试");
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!current) return;
    setBusy(true);
    try {
      await deleteQuickAsk(current.docId);
    } catch {
      /* 删除失败不阻塞前端：后端有 TTL 自动清理 */
    } finally {
      setBusy(false);
      onDetach();
    }
  };

  return (
    <div className="quick-attach-panel" role="dialog" aria-label="附件提问">
      <div className="quick-attach-head">
        <strong>附件提问</strong>
        <button type="button" className="btn-text" onClick={onClose} aria-label="关闭">
          ✕
        </button>
      </div>

      {current ? (
        <div className="quick-attach-current">
          <span className="trace-id">已附加：{current.filename}（{current.chunks} 个片段）</span>
          <button type="button" className="btn-text" disabled={busy} onClick={() => void handleDelete()}>
            移除
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="uploader-zone"
          disabled={busy}
          onClick={() => inputRef.current?.click()}
        >
          {busy
            ? "上传并解析中…"
            : "点击选择文件（PDF / Word / Excel / PPT / Markdown / 纯文本 / 代码）"}
        </button>
      )}

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPT_ATTRIBUTE}
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) void handleFile(file);
        }}
      />

      {error && (
        <div className="error-inline" role="alert">
          <span aria-hidden>⚠</span>
          <span>{error}</span>
        </div>
      )}

      <p className="trace-id">
        该文件<b>不会</b>进入知识库，2 小时后自动清理；接下来的提问只检索这一个文件。
      </p>
    </div>
  );
}

export default QuickAttachPanel;
