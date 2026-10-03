/** 流式渲染文本（阶段 1 §2.5）：识别 [#N] 自动转 CitationChip，
 * 流式期间末尾显示闪烁光标。
 */
import { CitationChip } from "./CitationChip";

interface Props {
  content: string;
  isStreaming?: boolean;
  enableCitations?: boolean;
  onCitationClick?: (rank: number) => void;
}

const CITATION_RE = /\[#(\d+)\]/;

export function StreamText({ content, isStreaming, enableCitations = true, onCitationClick }: Props) {
  const parts = enableCitations ? content.split(/(\[#\d+\])/g) : [content];
  return (
    <div className="answer-text" aria-live="polite">
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
