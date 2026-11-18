/** 消息气泡：卡片分两个层级——
 *
 * ① 最终结果（主区，常驻显示）：答案正文 + 引用芯片；
 * ② 中间过程（默认折叠）：检索链路统计 + 证据切片 + 相关度分，
 *    点击"检索过程与证据"展开；点击答案中的引用编号会自动展开并定位到对应证据。
 */
import { memo, useEffect, useMemo, useRef, useState } from "react";
import type { ChatMessage, Evidence } from "../../../types";
import { downloadAnswerAsTxt } from "../../../lib/export-txt";
import { AgentTraceTimeline } from "./AgentTraceTimeline";
import { ConfidenceBar } from "./ConfidenceBar";
import { EvidenceCard } from "./EvidenceCard";
import { EvidenceScoreTable } from "./EvidenceScoreTable";
import { ParametricNotice } from "./ParametricNotice";
import { QueryAnalysisPanel } from "./QueryAnalysisPanel";
import { RefusalCard } from "./RefusalCard";
import { RetrievalTracePanel } from "./RetrievalTracePanel";
import { StreamText } from "./StreamText";

interface Props {
  message: ChatMessage;
  onCitationClick: (messageId: string, rank: number) => void;
  onViewOriginal: (evidence: Evidence) => void;
  onRetry?: (messageId: string) => void;
  onReask?: (question: string) => void;
  /** F5：点击推荐追问（直接发问） */
  onFollowup?: (question: string) => void;
  originalQuestion?: string;
  highlightRank?: number | null;
}

const COLLAPSE_THRESHOLD = 3;

/** `memo` 是长会话的**前提**：流式输出时每个 token 都会重建 `messages` 数组，
 * 没有 memo 的话 100 条历史消息会跟着一起重渲染（证据卡片、时间线全在里面）。
 *
 * 生效条件：父级传进来的回调必须是稳定引用（`useCallback`）。只要有一个内联
 * 箭头函数，memo 就等于没加——这是这类优化最常见的失效方式，改 ChatPage 时
 * 不要随手把 `onReask={(q) => send(q)}` 写回去。 */
export const MessageBubble = memo(function MessageBubble({
  message,
  onCitationClick,
  onViewOriginal,
  onRetry,
  onReask,
  onFollowup,
  originalQuestion,
  highlightRank,
}: Props) {
  const [expandedRanks, setExpandedRanks] = useState<Set<number>>(new Set());
  const [showAll, setShowAll] = useState(false);
  const [copied, setCopied] = useState(false);
  const [processOpen, setProcessOpen] = useState(false); // 中间过程默认折叠
  const [evidenceQuery, setEvidenceQuery] = useState(""); // 引文面板内的关键字过滤
  const containerRef = useRef<HTMLDivElement>(null);

  // 参考汇总：N 条引文来自 M 个文件（按 doc_id 去重，文件名可能为空）
  const citationSummary = useMemo(() => {
    const files = new Set<string>();
    for (const ev of message.evidence) {
      files.add(ev.doc_id || ev.filename || "");
    }
    return { count: message.evidence.length, files: files.size };
  }, [message.evidence]);

  // 引文过滤：内容 / 来源 / 文件名 / chunk_id 任一命中即可
  const filteredEvidence = useMemo(() => {
    const q = evidenceQuery.trim().toLowerCase();
    if (!q) return message.evidence;
    return message.evidence.filter((ev) =>
      [ev.content, ev.source, ev.filename, ev.doc_id, ev.chunk_id]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(q)),
    );
  }, [message.evidence, evidenceQuery]);

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
    ? filteredEvidence
    : filteredEvidence.slice(0, COLLAPSE_THRESHOLD);

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
          {/* 循环计数与熔断标识 */}
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
          {/* 通用知识兜底：答案不来自知识库，来源必须显式标注（诚实性原则） */}
          {message.answerSource === "parametric" && (
            <span className="parametric-badge" title="知识库未收录相关内容，以下由大模型自身知识回答">
              模型自身知识
            </span>
          )}
        </div>

        {message.answerSource === "parametric" && !message.streaming && (
          <ParametricNotice traceId={message.traceId} />
        )}

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
              <button type="button" className="btn-text" onClick={() => onRetry(message.id)}>
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

        {/* 参考汇总：N 条引文来自 M 个文件（对齐 doc-qa 的"参考"卡片） */}
        {!message.streaming && !message.error && citationSummary.count > 0 && (
          <div className="citation-summary" aria-label="参考">
            <span className="citation-summary-text">
              {citationSummary.count} 条引文 · 来自 {citationSummary.files} 个文件
            </span>
            <button
              type="button"
              className="btn-text"
              onClick={() => {
                setProcessOpen(true);
                setTimeout(() => {
                  containerRef.current
                    ?.querySelector(".evidence-list")
                    ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
                }, 60);
              }}
            >
              查看引文
            </button>
          </div>
        )}

        {!message.streaming && !message.error && message.content && (
          <div className="msg-actions">
            <button type="button" className="btn-text" onClick={() => copyText(message.content)}>
              {copied ? "已复制" : "复制"}
            </button>
            <button
              type="button"
              className="btn-text"
              onClick={() => downloadAnswerAsTxt(message, originalQuestion)}
              aria-label="导出 TXT"
            >
              导出 TXT
            </button>
          </div>
        )}

        {/* F5：推荐追问（点击即发） */}
        {!message.streaming && !message.error && (message.followups?.length ?? 0) > 0 && (
          <div className="followup-list" aria-label="推荐追问">
            {message.followups!.map((q, i) => (
              <button
                key={`${i}-${q}`}
                type="button"
                className="followup-btn"
                onClick={() => onFollowup?.(q)}
              >
                {q}
              </button>
            ))}
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
                {message.analysis && <QueryAnalysisPanel analysis={message.analysis} />}
                <RetrievalTracePanel
                  stats={message.retrieval ?? null}
                  fromCache={message.fromCache}
                  reranked={message.reranked}
                  crag={message.crag}
                  refineFrom={message.refineFrom}
                  refineTo={message.refineTo}
                />
                {message.evidence.length > 0 && (message.retrieval || message.confidence != null || message.crag) && (
                  <details
                    className="evidence-score-wrap"
                    open={(message.confidence ?? 1) < 0.5}
                  >
                    <summary className="trace-id">
                      证据相关度评分（{message.evidence.length} 条）
                    </summary>
                    <EvidenceScoreTable evidence={message.evidence} />
                  </details>
                )}
                {message.evidence.length > 0 && (
                  <div className="evidence-list">
                    <div className="evidence-search">
                      <input
                        className="input"
                        type="search"
                        value={evidenceQuery}
                        placeholder="在引文中搜索关键字…"
                        aria-label="在引文中搜索"
                        onChange={(e) => {
                          setEvidenceQuery(e.target.value);
                          setShowAll(false);
                        }}
                      />
                      {evidenceQuery.trim() && (
                        <span className="trace-id">
                          匹配 {filteredEvidence.length} / {message.evidence.length} 条
                        </span>
                      )}
                    </div>
                    {filteredEvidence.length === 0 && (
                      <div className="trace-id">没有匹配的引文</div>
                    )}
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
                    {filteredEvidence.length > COLLAPSE_THRESHOLD && (
                      <button type="button" className="btn-text" onClick={() => setShowAll(!showAll)}>
                        {showAll ? "收起" : `展开全部 ${filteredEvidence.length} 条证据`}
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
});
