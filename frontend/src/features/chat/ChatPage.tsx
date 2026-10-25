/** 对话页（/）：问答主流程 —— 提问 → Agent 轨迹 → 流式渲染 → 引用跳转。
 *
 *  增量：
 * - 默认走 Agent 状态机（mode=agent），实时渲染 plan/tool/reflect 轨迹；
 * - 多轮会话：侧边栏列表/新建/切换/重命名/删除，证据跨轮累积；
 * - 熔断 → 黄色"已达上限"提示（不完整的成功，不是错误）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  createSession,
  deleteSession,
  getSession,
  listSessions,
  renameSession,
} from "../../lib/api-client";
import { askStream } from "../../lib/sse-client";
import { ChatInput, type ChatInputHandle } from "./components/ChatInput";
import { Markdown } from "./components/Markdown";
import { MessageBubble } from "./components/MessageBubble";
import { QuickAttachPanel, type QuickDoc } from "./components/QuickAttachPanel";
import { SessionSidebar } from "./components/SessionSidebar";
import { VoiceRecorder } from "./components/VoiceRecorder";
import type {
  ChatMessage,
  CragRound,
  Evidence,
  RefusalReason,
  RetrievalStats,
  SessionSummary,
  TraceNode,
} from "../../types";

const SAMPLE_QUESTIONS = ["什么是机会成本？请举例说明", "货币市场工具主要有哪些类型？", "价格在资源配置中起什么作用？"];

let msgSeq = 0;
const nextId = () => `m${++msgSeq}`;

interface RateLimitState {
  secondsLeft: number;
  question: string;
}

export function ChatPage() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [highlight, setHighlight] = useState<{ messageId: string; rank: number } | null>(null);
  const [preview, setPreview] = useState<Evidence | null>(null);
  const [rateLimit, setRateLimit] = useState<RateLimitState | null>(null);
  // ---- 会话状态 ----
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSession, setActiveSession] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // ---- F4 快问：当前临时文档（提问时随请求带上 doc_id）----
  const [quickDoc, setQuickDoc] = useState<QuickDoc | null>(null);
  const [attachOpen, setAttachOpen] = useState(false);
  // ---- F2 语音：转写文本回填输入框 ----
  const inputRef = useRef<ChatInputHandle>(null);

  const patchMessage = useCallback((id: string, patch: Partial<ChatMessage>) => {
    setMessages((prev) => prev.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }, []);

  const refreshSessions = useCallback(async () => {
    const envelope = await listSessions();
    if (envelope.data) setSessions(envelope.data);
  }, []);

  useEffect(() => {
    void refreshSessions();
  }, [refreshSessions]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  // 限流倒计时：1s 刷新，归零自动重试一次
  useEffect(() => {
    if (!rateLimit) return;
    if (rateLimit.secondsLeft <= 0) {
      const q = rateLimit.question;
      setRateLimit(null);
      send(q);
      return;
    }
    const timer = setTimeout(
      () => setRateLimit((prev) => (prev ? { ...prev, secondsLeft: prev.secondsLeft - 1 } : null)),
      1000,
    );
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rateLimit]);

  /** 首次提问时自动建会话（会话标识 = 租户+用户+随机号，服务端生成） */
  const ensureSession = useCallback(async (): Promise<string | null> => {
    if (activeSession) return activeSession;
    const envelope = await createSession();
    if (!envelope.data) return null;
    setActiveSession(envelope.data.session_id);
    void refreshSessions();
    return envelope.data.session_id;
  }, [activeSession, refreshSessions]);

  const send = useCallback(
    async (question: string) => {
      if (streaming) return;
      const sessionId = await ensureSession();
      const userId = nextId();
      const assistantId = nextId();
      setMessages((prev) => [
        ...prev,
        { id: userId, role: "user", content: question, evidence: [] },
        { id: assistantId, role: "assistant", content: "", evidence: [], streaming: true, traceNodes: [] },
      ]);
      setStreaming(true);
      const controller = new AbortController();
      abortRef.current = controller;

      // ---- 轨迹节点操作 ----
      const pushNode = (node: TraceNode) =>
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantId ? { ...m, traceNodes: [...(m.traceNodes ?? []), node] } : m,
          ),
        );
      const patchNode = (stepIndex: number, patch: Partial<TraceNode>) =>
        setMessages((prev) =>
          prev.map((m) => {
            if (m.id !== assistantId) return m;
            const nodes = (m.traceNodes ?? []).map((n) =>
              n.kind === "tool" && n.stepIndex === stepIndex ? { ...n, ...patch } : n,
            );
            return { ...m, traceNodes: nodes };
          }),
        );

      void askStream(question, controller.signal, {
        onToken: (content) =>
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, content: m.content + content } : m)),
          ),
        onEvidence: (ev) =>
          setMessages((prev) =>
            prev.map((m) => (m.id === assistantId ? { ...m, evidence: [...m.evidence, ev] } : m)),
          ),
        // ---- 检索过程与自反思----
// 这四个 handler 此前一律写成空函数，导致 RetrievalTracePanel / 缓存徽章 /
// CRAG 分支高亮 / ConfidenceBar 在真实问答中全部拿不到数据（死代码）。
        onRetrievalStats: (stats) =>
          patchMessage(assistantId, { retrieval: stats as RetrievalStats }),
        onQueryAnalysis: (analysis) => patchMessage(assistantId, { analysis }),
        onCragBranch: ({ branch, avg_score, round }) =>
          setMessages((prev) =>
            prev.map((m) => {
              if (m.id !== assistantId) return m;
              const rounds: NonNullable<ChatMessage["crag"]>["rounds"] = [
                ...(m.crag?.rounds ?? []),
                {
                  round,
                  // 后端 branch 是自由字符串，收敛到前端枚举：
                  // 未知值按 ambiguous 处理（比当成 correct 更保守）
                  branch: (["correct", "ambiguous", "incorrect"].includes(branch)
                    ? branch
                    : "ambiguous") as CragRound["branch"],
                  avg_score,
                  evidence_count: 0,
                  refined_query: "",
                },
              ];
              return { ...m, crag: { branch: rounds[rounds.length - 1].branch, rounds } };
            }),
          ),
        onRefine: ({ before, after }) =>
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantId ? { ...m, refineFrom: before, refineTo: after } : m,
            ),
          ),
        onConfidence: ({ score, unsupported_claims_count, regenerated, unvalidated, removed_claims, answer_source }) =>
          patchMessage(assistantId, {
            confidence: score ?? undefined,
            unsupportedClaimsCount: unsupported_claims_count,
            regenerated,
            unvalidated,
            removedClaims: removed_claims ?? [],
            ...(answer_source ? { answerSource: answer_source } : {}),
          }),
        // ---- Agent 轨迹事件 ----
        onPlan: ({ steps, replan }) =>
          pushNode({
            kind: replan ? "replan" : "plan",
            title: replan ? `重新规划：生成 ${steps.length} 步骤计划` : `生成 ${steps.length} 步骤计划`,
            detail: steps.map((s) => `${s.step_index}. ${s.description}（${s.tool_name}）`).join("；"),
          }),
        onToolStart: ({ step_index, tool, input }) =>
          pushNode({
            kind: "tool", stepIndex: step_index, tool, input,
            title: `${tool}  “${input.slice(0, 40)}”`, running: true,
          }),
        onToolEnd: ({ step_index, elapsed_ms, hit_count, error, evidence: stepEvidence }) => {
          patchNode(step_index, {
            running: false, elapsedMs: elapsed_ms, hitCount: hit_count,
            error: error || undefined,
          });
          // 该步证据实时累积（最终编号以 done 事件为准）
          if (stepEvidence?.length) {
            setMessages((prev) =>
              prev.map((m) => {
                if (m.id !== assistantId) return m;
                const existing = new Set(m.evidence.map((e) => e.chunk_id));
                const fresh = stepEvidence
                  .filter((e) => !existing.has(e.chunk_id))
                  .map((e, i) => ({ ...e, rank: m.evidence.length + i + 1 }));
                return { ...m, evidence: [...m.evidence, ...fresh] };
              }),
            );
          }
        },
        onReflect: ({ evidence_score, next_action, reason }) =>
          pushNode({
            kind: "reflect",
            title: "评估证据",
            detail: `evidence_score: ${evidence_score.toFixed(2)}  next_action: ${next_action}${reason ? `（${reason}）` : ""}`,
            score: evidence_score, nextAction: next_action,
          }),
        onFuse: ({ reason }) => {
          pushNode({ kind: "reflect", title: "⚠ 触发熔断", detail: reason === "no_progress" ? "连续多轮无新证据" : "达到迭代上限" });
          patchMessage(assistantId, { fuseReason: reason });
        },
        onRefused: ({ reason }) =>
          patchMessage(assistantId, {
            verdict: "refused", refusalReason: reason, confidence: 0,
          }),
        // F5：推荐追问（后端在 done 之前发出）
        onFollowups: ({ items }) =>
          patchMessage(assistantId, { followups: items ?? [] }),
        onDone: ({ trace_id, agent, incomplete, fuse_reason, session_id, evidence, verdict, confidence, acl_filtered, retrieval, crag, from_cache, refusal_reason, regenerated, unsupported_claims_count, answer_source }) => {
          patchMessage(assistantId, {
            streaming: false, traceId: trace_id,
            ...(answer_source ? { answerSource: answer_source } : {}),
            agent: agent ?? null, incomplete, fuseReason: fuse_reason,
            // 最终编号后的证据替换临时累积（rank 从 1 连续，与答案引用对位）
            ...(evidence ? { evidence } : {}),
            ...(verdict ? { verdict: verdict as ChatMessage["verdict"], confidence } : {}),
            ...(acl_filtered ? { aclFiltered: acl_filtered } : {}),
            // 缓存命中徽章：此前 Agent 路径硬编码 false，徽章永不出现
            fromCache: from_cache === true,
            retrieval: retrieval ?? undefined,
            crag: crag ?? undefined,
            refusalReason: (refusal_reason as RefusalReason) || undefined,
            regenerated: regenerated === true,
            unsupportedClaimsCount: unsupported_claims_count,
          });
          if (session_id && session_id !== activeSession) setActiveSession(session_id);
          void refreshSessions();
        },
        onError: (error) => {
          if (error.code === 6101 && error.retry_after) {
            setMessages((prev) => prev.filter((m) => m.id !== assistantId && m.id !== userId));
            setRateLimit({ secondsLeft: error.retry_after, question });
          } else if (error.code === 4301 || error.code === 4302) {
            // 会话失效：提示并开启新对话
            setActiveSession(null);
            patchMessage(assistantId, {
              streaming: false,
              error: { code: error.code, message: "会话已失效，请重新提问（将开启新对话）" },
            });
          } else {
            patchMessage(assistantId, { streaming: false, error, traceId: undefined });
          }
        },
      }, { sessionId, mode: "agent", docId: quickDoc?.docId ?? null }).finally(() => setStreaming(false));
    },
    [streaming, patchMessage, ensureSession, activeSession, refreshSessions, quickDoc],
  );

  const stop = useCallback(() => {
    abortRef.current?.abort();
    setStreaming(false);
    setMessages((prev) => prev.map((m) => (m.streaming ? { ...m, streaming: false } : m)));
  }, []);

  const retry = useCallback(
    (failedId: string) => {
      const idx = messages.findIndex((m) => m.id === failedId);
      for (let i = idx - 1; i >= 0; i--) {
        if (messages[i].role === "user") {
          send(messages[i].content);
          return;
        }
      }
    },
    [messages, send],
  );

  // ---- 会话操作 ----

  const handleSelectSession = useCallback(async (id: string) => {
    if (streaming) return;
    const envelope = await getSession(id);
    if (!envelope.data) return;
    setActiveSession(id);
    // 从会话存档恢复消息（证据可溯恢复快照，此处恢复问答对）
    const restored: ChatMessage[] = [];
    for (const turn of envelope.data.turns) {
      restored.push({ id: nextId(), role: "user", content: turn.question, evidence: [] });
      restored.push({
        id: nextId(), role: "assistant", content: turn.answer, evidence: [],
        verdict: turn.verdict as ChatMessage["verdict"],
        answerSource: turn.answer_source as ChatMessage["answerSource"],
      });
    }
    setMessages(restored);
  }, [streaming]);

  const handleCreateSession = useCallback(() => {
    setActiveSession(null);
    setMessages([]);
  }, []);

  const handleRenameSession = useCallback(async (id: string, title: string) => {
    await renameSession(id, title);
    void refreshSessions();
  }, [refreshSessions]);

  const handleDeleteSession = useCallback(async (id: string) => {
    await deleteSession(id);
    if (id === activeSession) {
      setActiveSession(null);
      setMessages([]);
    }
    void refreshSessions();
  }, [activeSession, refreshSessions]);

  const handleCitationClick = useCallback((messageId: string, rank: number) => {
    setHighlight({ messageId, rank });
    setTimeout(() => setHighlight(null), 2100);
  }, []);

  return (
    <div className="chat-with-sessions">
      <SessionSidebar
        sessions={sessions}
        activeId={activeSession}
        onSelect={(id) => void handleSelectSession(id)}
        onCreate={handleCreateSession}
        onRename={(id, title) => void handleRenameSession(id, title)}
        onDelete={(id) => void handleDeleteSession(id)}
      />
      <div className="chat-main">
        {rateLimit && (
          <div className="ratelimit-banner" role="alert">
            <span aria-hidden>⏱</span>
            [限流] 当前租户配额已用完，
            <strong className="mono">{rateLimit.secondsLeft}</strong> 秒后自动重试
          </div>
        )}
        <div className="chat-scroll" ref={scrollRef}>
          <div className="chat-inner">
            {messages.length === 0 ? (
              <div className="empty-state">
                <h2>你好，我是 Kquery 知识库助手</h2>
                <p>基于知识库中的经济学教材回答，并给出引用来源。可以先试试：</p>
                <div className="samples">
                  {SAMPLE_QUESTIONS.map((q) => (
                    <button key={q} type="button" className="sample-q" onClick={() => void send(q)}>
                      {q}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              messages.map((m, idx) => (
                <MessageBubble
                  key={m.id}
                  message={m}
                  originalQuestion={idx > 0 ? messages[idx - 1]?.content : undefined}
                  highlightRank={highlight?.messageId === m.id ? highlight.rank : null}
                  onCitationClick={handleCitationClick}
                  onViewOriginal={setPreview}
                  onRetry={m.error ? () => retry(m.id) : undefined}
                  onReask={(q) => void send(q)}
                  onFollowup={(q) => void send(q)}
                />
              ))
            )}
          </div>
        </div>

        <div className="chat-input-area">
          {attachOpen && (
            <QuickAttachPanel
              current={quickDoc}
              onAttach={(doc) => {
                setQuickDoc(doc);
                setAttachOpen(false);
              }}
              onDetach={() => setQuickDoc(null)}
              onClose={() => setAttachOpen(false)}
              sessionId={activeSession}
            />
          )}
          <ChatInput
            ref={inputRef}
            disabled={false}
            streaming={streaming}
            onSend={(q) => void send(q)}
            onStop={stop}
            onAttach={() => setAttachOpen((v) => !v)}
            voice={<VoiceRecorder onTranscript={(text) => inputRef.current?.append(text)} />}
          />
          {quickDoc && !attachOpen && (
            <div className="quick-attach-bar trace-id">
              附件提问中：{quickDoc.filename}（{quickDoc.chunks} 片段）
              <button type="button" className="btn-text" onClick={() => setQuickDoc(null)}>
                取消
              </button>
            </div>
          )}
        </div>
      </div>

      {preview && (
        <div className="modal-mask" onClick={() => setPreview(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="原文预览">
            <h3>
              [#{preview.rank}] {preview.source}
            </h3>
            <p className="trace-id mono">chunk_id: {preview.chunk_id}</p>
            {/* 图像证据大图查看器：保留溯源信息 */}
            {preview.modality === "image" && preview.uri && (
              <div style={{ textAlign: "center", marginBottom: 12 }}>
                <img
                  src={`/assets/${encodeURIComponent(preview.uri.split("/").pop() ?? "")}`}
                  alt={preview.content.slice(0, 50)}
                  style={{ maxWidth: "100%", maxHeight: 400, borderRadius: 6 }}
                />
              </div>
            )}
            <div className="answer-text">
              <Markdown value={preview.content} enableCitations={false} />
            </div>
            <div style={{ marginTop: 16, textAlign: "right" }}>
              <button type="button" className="btn" onClick={() => setPreview(null)}>
                关闭
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
