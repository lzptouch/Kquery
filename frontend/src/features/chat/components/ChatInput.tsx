/** 问题输入框（阶段 1 §2.6）：自适应高度，Enter 发送 / Shift+Enter 换行，
 * 草稿本地存储防刷新丢失，附件按钮阶段 1 禁用（阶段 6 启用）。
 */
import { useEffect, useRef, useState } from "react";

interface Props {
  placeholder?: string;
  disabled?: boolean;
  onSend: (question: string) => void;
  onStop?: () => void;
  streaming?: boolean;
}

const DRAFT_KEY = "kquery.chat.draft";

export function ChatInput({ placeholder = "输入你的问题…", disabled, onSend, onStop, streaming }: Props) {
  const [value, setValue] = useState(() => localStorage.getItem(DRAFT_KEY) ?? "");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    localStorage.setItem(DRAFT_KEY, value);
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
    }
  }, [value]);

  const submit = () => {
    const question = value.trim();
    if (!question || disabled || streaming) return;
    if (question.length > 2000) return; // 就地提示由父级处理前的硬约束
    onSend(question);
    setValue("");
  };

  return (
    <div className="chat-input-box">
      <button type="button" className="btn" disabled title="附件提问（阶段 6 引入）" aria-label="附件（暂未开放）">
        📎
      </button>
      <textarea
        ref={textareaRef}
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
        aria-label="问题输入框"
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
      />
      {streaming ? (
        <button type="button" className="btn" onClick={onStop} aria-label="停止生成">
          ■ 停止
        </button>
      ) : (
        <button
          type="button"
          className="btn btn-primary"
          disabled={disabled || !value.trim()}
          onClick={submit}
          aria-label="发送"
        >
          发送
        </button>
      )}
    </div>
  );
}
