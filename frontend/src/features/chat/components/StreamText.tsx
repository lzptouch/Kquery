/** 流式渲染文本：识别 [#N] 自动转 CitationChip，
 * 流式期间末尾显示闪烁光标。
 *
 * 两个此前缺失的关键点：
 * 1. **requestAnimationFrame 节流**：SSE 每来一个 token 就同步重渲染整段文本，
 *    长答案下会逐帧重排，表现为明显的掉帧。文档 §2.5 明写要求 rAF 调度。
 * 2. **aria-live 只在流式结束后开启**：挂在流式期间会让屏幕阅读器每收一个 token
 *    就重播全文，形成播报风暴。正确做法是结束后给一份完整文本。
 */
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { CitationChip } from "./CitationChip";

interface Props {
  content: string;
  isStreaming?: boolean;
  enableCitations?: boolean;
  onCitationClick?: (rank: number) => void;
}

const CITATION_RE = /\[#(\d+)\]/;

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

  const parts = useMemo(
    () => (enableCitations ? throttled.split(/(\[#\d+\])/g) : [throttled]),
    [throttled, enableCitations],
  );

  return (
    // 流式期间关闭 aria-live，结束后再开启完整播报
    <div className="answer-text" aria-live={isStreaming ? "off" : "polite"} aria-busy={!!isStreaming}>
      {parts.map((part, i) => {
        const m = enableCitations ? part.match(CITATION_RE) : null;
        if (m) {
          return <CitationChip key={i} rank={Number(m[1])} onClick={onCitationClick} />;
        }
        return <span key={i}>{part}</span>;
      })}
      {isStreaming && <span className="stream-cursor" aria-hidden />}
    </div>
  );
}

// memo：token 到达时父组件会重渲染消息列表，未 memo 的证据卡/时间轴会跟着全量重渲染
export const StreamText = memo(StreamTextInner);
export default StreamText;
