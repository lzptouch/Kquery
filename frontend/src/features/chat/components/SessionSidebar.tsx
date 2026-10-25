/** 多轮会话侧边栏。
 *
 * - 列表 + 切换 + 重命名 + 删除（二次确认，同时清理会话存档）；
 * - 当前会话左侧蓝色竖条；悬停出现操作按钮。
 */
import { useState } from "react";
import type { SessionSummary } from "../../../types";

function formatRelative(ts: number): string {
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

export function SessionSidebar({
  sessions,
  activeId,
  onSelect,
  onCreate,
  onRename,
  onDelete,
}: {
  sessions: SessionSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
}) {
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");

  return (
    <aside className="session-sidebar" aria-label="会话列表">
      <button type="button" className="session-new" onClick={onCreate}>
        ＋ 新对话
      </button>
      <div className="session-list">
        {sessions.map((s) => (
          <div
            key={s.session_id}
            className={`session-item${s.session_id === activeId ? " active" : ""}`}
            onMouseEnter={() => setHoverId(s.session_id)}
            onMouseLeave={() => setHoverId(null)}
          >
            {editingId === s.session_id ? (
              <input
                className="session-rename-input"
                value={editTitle}
                autoFocus
                onChange={(e) => setEditTitle(e.target.value)}
                onBlur={() => {
                  if (editTitle.trim()) onRename(s.session_id, editTitle.trim());
                  setEditingId(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                  if (e.key === "Escape") setEditingId(null);
                }}
              />
            ) : (
              <button type="button" className="session-main" onClick={() => onSelect(s.session_id)}>
                <span className="session-title">{s.title}</span>
                <span className="session-meta">
                  {s.message_count} 条消息 · {formatRelative(s.updated_at)}
                </span>
              </button>
            )}
            {hoverId === s.session_id && editingId !== s.session_id && (
              <span className="session-ops">
                <button
                  type="button"
                  title="重命名"
                  onClick={() => {
                    setEditingId(s.session_id);
                    setEditTitle(s.title);
                  }}
                >
                  ✎
                </button>
                <button
                  type="button"
                  title="删除（连同会话存档）"
                  onClick={() => {
                    if (window.confirm(`删除会话「${s.title}」？会话存档将一并删除。`)) {
                      onDelete(s.session_id);
                    }
                  }}
                >
                  ✕
                </button>
              </span>
            )}
          </div>
        ))}
        {sessions.length === 0 && <div className="session-empty">暂无会话，点击"新对话"开始</div>}
      </div>
    </aside>
  );
}
