/** 消息气泡：卡片分两个层级——
 *
 * ① 最终结果（主区，常驻显示）：答案正文 + 引用芯片；
 * ② 中间过程（默认折叠）：检索链路统计 + 证据切片 + 相关度分，
 *    点击"检索过程与证据"展开；点击答案中的引用编号会自动展开并定位到对应证据。
 */
import { useEffect, useRef, useState } from "react";
import type { ChatMessage, Evidence } from "../../../types";
import { AgentTraceTimeline } from "./AgentTraceTimeline";
import { ConfidenceBar } from "./ConfidenceBar";
import { EvidenceCard } from "./EvidenceCard";
import { QueryAnalysisPanel } from "./QueryAnalysisPanel";
import { RefusalCard } from "./RefusalCard";
import { RetrievalTracePanel } from "./RetrievalTracePanel";
import { StreamText } from "./StreamText";

interface Props {
  message: ChatMessage;
  onCitationClick: (messageId: string, rank: number) => void;
  onViewOriginal: (evidence: Evidence) => void;
  onRetry?: () => void;
  onReask?: (question: string) => void;
  originalQuestion?: string;
  highlightRank?: number | null;
}

const COLLAPSE_THRESHOLD = 3;

export function MessageBubble({ message, onCitationClick, onViewOriginal, onRetry, onReask, originalQuestion, highlightRank }: Props) {
  const [expandedRanks, setExpandedRanks] = useState<Set<number>>(new Set());
  const [showAll, setShowAll] = useState(false);
  const [copied, setCopied] = useState(false);
  const [processOpen, setProcessOpen] = useState(false); // 中间过程默认折叠
  const containerRef = useRef<HTMLDivElement>(null);

  // 流式期间出现轨迹 → 自动展开过程区（执行五态可见，§3.1）
  const traceCount = message.traceNodes?.length ?? 0;
  useEffect(() => {
    if (message.streaming && traceCount > 0) setProcessOpen(true);
  }, [message.streaming, traceCount]);

  // 引用溯源：点击引用编号 → 展开中间过程 → 展开对应证据并平滑滚动定位
  useEffect(() => {
    if (highlightRank == null) return;
    setProcessOpen(true);
    setExpandedRanks((prev) => new Set(prev).add(highlightRank));
    const timer = setTimeout(() => {
      containerRef.current
        ?.querySelector(`[data-rank="${highlightRank}"]`)
        ?.scrollIntoView({ behavior: "smooth", block: "center" });
    }, 80);
    return () => clearTimeout(timer);
  }, [highlightRank]);

  if (message.role === "user") {
    return (
      <div className="msg-row user">
        <div className="bubble-user">{message.content}</div>
      </div>
    );
  }

  const toggleRank = (rank: number) => {
    setExpandedRanks((prev) => {
      const next = new Set(prev);
      if (next.has(rank)) next.delete(rank);
      else next.add(rank);
      return next;
    });
  };

  const copyText = async (text: string) => {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const hasProcess =
    message.evidence.length > 0 ||
    message.retrieval != null ||
    message.fromCache ||
    message.analysis != null ||
    (message.traceNodes?.length ?? 0) > 0;
  const visibleEvidence = showAll
    ? message.evidence
    : message.evidence.slice(0, COLLAPSE_THRESHOLD);

  // 拒答是一等公民：渲染拒答卡片（不是错误态）
  if (message.verdict === "refused" && !message.streaming) {
    return (
      <div className="msg-row">
        <div className="answer-card" ref={containerRef}>
          <RefusalCard
            reason={message.refusalReason ?? "no_content"}
            traceId={message.traceId}
            originalQuestion={originalQuestion ?? ""}
            onReask={onReask ?? (() => {})}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="msg-row">
      <div className="answer-card" ref={containerRef}>
        {/* ===== ① 最终结果（主区，常驻） ===== */}
        <div className="answer-head">
          <span className="answer-label">回答</span>
          {message.fromCache && (
            <span className="cache-badge" title="语义缓存命中：相同问题在 TTL 内直接复用响应">
              ⚡ 来自缓存
            </span>
          )}
          {message.reranked === false && !message.fromCache && (
            <span className="route-tag route-keyword" title="重排服务降级，本次未精排">
              未精排
            </span>
          )}
          {/* 循环计数与熔断标识（阶段 6 §1.3） */}
          {message.agent && message.agent.iterations > 0 && (
            <span className="trace-id mono" title={`迭代 ${message.agent.iterations}/${message.agent.max_iterations}`}>
              🔁 循环 {message.agent.iterations} 轮
            </span>
          )}
          {(message.incomplete || message.fuseReason) && (
            <span className="fuse-badge" title="达到迭代上限，结果可能不完整">
              ⚠ 已达上限
            </span>
          )}
        </div>

        {(message.confidence !== undefined || message.regenerated) && !message.streaming && (
          <ConfidenceBar
            score={message.confidence ?? null}
            unsupportedClaimsCount={message.unsupportedClaimsCount}
            regenerated={message.regenerated}
          />
        )}

        {message.content ? (
          <StreamText
            content={message.content}
            isStreaming={message.streaming}
            onCitationClick={(rank) => onCitationClick(message.id, rank)}
          />
        ) : message.streaming ? (
          <span className="typing-dots">思考中</span>
        ) : !message.error ? (
          <span className="trace-id">（未生成答案内容，请重试）</span>
        ) : null}

        {message.error && (
          <div className="error-inline" role="alert">
            <span aria-hidden>⚠</span>
            <span>{message.error.message}</span>
            {onRetry && (
              <button type="button" className="btn-text" onClick={onRetry}>
                重试
              </button>
            )}
            {message.traceId && (
              <button
                type="button"
                className="trace-id mono copyable"
                title="点击复制追踪号，用于问题反馈"
                onClick={() => copyText(message.traceId!)}
              >
                trace: {message.traceId}
              </button>
            )}
          </div>
        )}

        {!message.streaming && !message.error && message.content && (
          <div className="msg-actions">
            <button type="button" className="btn-text" onClick={() => copyText(message.content)}>
              {copied ? "已复制" : "复制"}
            </button>
          </div>
        )}

        {/* ===== ② 中间过程（默认折叠，点击展开） ===== */}
        {hasProcess && (
          <div className="process-section">
            <button
              type="button"
              className="process-toggle"
              aria-expanded={processOpen}
              onClick={() => setProcessOpen(!processOpen)}
            >
              <span aria-hidden>{processOpen ? "▾" : "▸"}</span>
              检索过程与证据
              {message.evidence.length > 0 && (
                <span className="trace-id">（{message.evidence.length} 条证据切片）</span>
              )}
              {message.streaming && <span className="trace-id">· 召回中…</span>}
            </button>

            {processOpen && (
              <div className="process-body">
                {(message.traceNodes?.length ?? 0) > 0 && (
                  <AgentTraceTimeline
                    nodes={message.traceNodes!}
                    fused={Boolean(message.incomplete || message.fuseReason)}
                  />
                )}
                {message.aclFiltered != null && message.aclFiltered > 0 && (
                  <div className="pending-note">
                    🔒 另有 {message.aclFiltered} 条结果因权限未展示。如需访问请联系管理员申请权限。
                  </div>
                )}
                {message.analysis && <QueryAnalysisPanel analysis={message.analysis} />}
                <RetrievalTracePanel
                  stats={message.retrieval ?? null}
                  fromCache={message.fromCache}
                  reranked={message.reranked}
                  crag={message.crag}
                />
                {message.evidence.length > 0 && (
                  <div className="evidence-list">
                    {visibleEvidence.map((ev) => (
                      <EvidenceCard
                        key={ev.rank}
                        evidence={ev}
                        expanded={expandedRanks.has(ev.rank)}
                        highlighted={highlightRank === ev.rank}
                        onToggle={() => toggleRank(ev.rank)}
                        onViewOriginal={onViewOriginal}
                      />
                    ))}
                    {message.evidence.length > COLLAPSE_THRESHOLD && (
                      <button type="button" className="btn-text" onClick={() => setShowAll(!showAll)}>
                        {showAll ? "收起" : `展开全部 ${message.evidence.length} 条证据`}
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
