/** 流式渲染文本（阶段 1 §2.5）：按 Markdown 渲染答案，[#N] 自动转可点击角标，
 * 流式期间末尾显示闪烁光标。
 *
 * 三个此前缺失的关键点：
 * 1. **requestAnimationFrame 节流**：SSE 每来一个 token 就同步重渲染整段文本，
 *    长答案下会逐帧重排，表现为明显的掉帧。文档 §2.5 明写要求 rAF 调度。
 * 2. **aria-live 只在流式结束后开启**：挂在流式期间会让屏幕阅读器每收一个 token
 *    就重播全文，形成播报风暴。正确做法是结束后给一份完整文本。
 * 3. **Markdown 渲染（F1）**：标题/列表/表格/代码块按 Markdown 出，
 *    角标仍是 `[#N]` 语义（后端产出格式未变），点击链路与改造前一致。
 */
import { memo, useEffect, useRef, useState } from "react";
import { Markdown } from "./Markdown";

interface Props {
  content: string;
  isStreaming?: boolean;
  enableCitations?: boolean;
  onCitationClick?: (rank: number) => void;
}

function StreamTextInner({ content, isStreaming, enableCitations = true, onCitationClick }: Props) {
  // rAF 节流：每帧最多渲染一次，token 再密也不会触发多次布局
  const [throttled, setThrottled] = useState(content);
  const frame = useRef<number | null>(null);
  useEffect(() => {
    if (!isStreaming) {
      setThrottled(content);
      return;
    }
    if (frame.current !== null) return; // 本帧已排队，等下一帧即可
    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      setThrottled(content);
    });
    return () => {
      if (frame.current !== null) {
        cancelAnimationFrame(frame.current);
        frame.current = null;
      }
    };
  }, [content, isStreaming]);

  return (
    // 流式期间关闭 aria-live，结束后再开启完整播报
    <div className="answer-text" aria-live={isStreaming ? "off" : "polite"} aria-busy={!!isStreaming}>
      <Markdown
        value={throttled}
        enableCitations={enableCitations}
        onCitationClick={onCitationClick}
      />
      {isStreaming && <span className="stream-cursor" aria-hidden />}
    </div>
  );
}

// memo：token 到达时父组件会重渲染消息列表，未 memo 的证据卡/时间轴会跟着全量重渲染
export const StreamText = memo(StreamTextInner);
export default StreamText;
