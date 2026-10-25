/** 问题输入框（阶段 1 §2.6）：自适应高度，Enter 发送 / Shift+Enter 换行，
 * 草稿本地存储防刷新丢失。
 *
 * F2/F4 增量：
 * - **命令式写入**（`ref.setValue`）：语音转写回填需要从外部改输入框内容。
 *   刻意不改造成"受控组件"——那样草稿本地存储就得挪到父组件，
 *   而草稿是既有能力，改动面大且与本次需求无关。
 * - 附件按钮从"永久 disabled"改为**可点击**（打开快问面板）。
 * - 语音按钮作为插槽（`voice`）挂在左侧，由 ChatPage 注入。
 */
import { forwardRef, useEffect, useImperativeHandle, useRef, useState, type ReactNode } from "react";

interface Props {
  placeholder?: string;
  disabled?: boolean;
  onSend: (question: string) => void;
  onStop?: () => void;
  streaming?: boolean;
  /** 点击附件按钮（快问） */
  onAttach?: () => void;
  /** 语音输入插槽 */
  voice?: ReactNode;
}

export interface ChatInputHandle {
  /** 从外部写入输入框内容（程序化改写） */
  setValue: (value: string) => void;
  /** 追加内容（语音转写：回填而不是覆盖用户已输入的字） */
  append: (value: string) => void;
}

const DRAFT_KEY = "kquery.chat.draft";

export const ChatInput = forwardRef<ChatInputHandle, Props>(function ChatInput(
  { placeholder = "输入你的问题…", disabled, onSend, onStop, streaming, onAttach, voice },
  ref,
) {
  const [value, setValue] = useState(() => localStorage.getItem(DRAFT_KEY) ?? "");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // 语音回填要"追加"而不是"覆盖"：需要拿到当前值的最新快照
  const valueRef = useRef(value);
  valueRef.current = value;

  useImperativeHandle(ref, () => ({
    setValue: (next: string) => {
      setValue(next);
      localStorage.setItem(DRAFT_KEY, next);
    },
    append: (part: string) => {
      const next = `${valueRef.current}${part}`;
      setValue(next);
      localStorage.setItem(DRAFT_KEY, next);
    },
  }), []);

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
    localStorage.setItem(DRAFT_KEY, "");
  };

  return (
    <div className="chat-input-box">
      {onAttach && (
        <button
          type="button"
          className="btn"
          disabled={disabled || streaming}
          onClick={onAttach}
          title="上传单个文件并只针对它提问（不入知识库）"
          aria-label="附件提问"
        >
          📎
        </button>
      )}
      {voice}
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
});

export default ChatInput;
