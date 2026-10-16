/** SSE 客户端（阶段 1 §4.2 + 阶段 2 §6 增量）。
 *
 * 协议纪律：
 * 1. 每类事件有唯一处理入口；
 * 2. 未知事件静默忽略（前后端可独立灰度）；
 * 3. done 与 error 互斥且必有其一；
 * 4. 容忍证据先于文本到达。
 *
 * 阶段 2 增量：retrieval_stats 事件（分阶段耗时）；
 * HTTP 429 时解析 Retry-After 并透传给界面（倒计时 + 自动重试）。
 */
import { authErrorMessage, isAuthFailure, withAuth } from "./auth";
import type {
  AgentPlanStep,
  Evidence,
  QueryAnalysis,
  RefusalReason,
  RetrievalStats,
  Verdict,
} from "../types";

export interface ConfidencePayload {
  verdict: Verdict;
  score: number | null;
  unsupported_claims_count: number;
  regenerated: boolean;
  unvalidated?: boolean;
  /** 阶段 5：被剔除的无据声明原文（前端要告诉用户剔除了哪句） */
  removed_claims?: string[];
  self_check?: import("../types").SelfCheckInfo | null;
  crag?: { branch: string; rounds: { round: number; branch: "correct" | "ambiguous" | "incorrect"; avg_score: number; evidence_count: number; refined_query: string }[] } | null;
}

export interface StreamHandlers {
  onToken: (content: string) => void;
  onEvidence: (evidence: Evidence) => void;
  onRetrievalStats: (stats: Partial<RetrievalStats> & { from_cache: boolean; reranked: boolean }) => void;
  onQueryAnalysis: (analysis: QueryAnalysis) => void;
  onCragBranch: (data: { branch: string; avg_score: number; round: number; acl_filtered?: number }) => void;
  onRefine: (data: { before: string; after: string }) => void;
  onConfidence: (data: ConfidencePayload) => void;
  onRefused: (data: { reason: RefusalReason; trace_id: string; acl_filtered?: number }) => void;
  onDone: (data: {
    trace_id: string;
    elapsed_ms: number;
    from_cache?: boolean;
    session_id?: string | null;
    agent?: import("../types").AgentTrace;
    incomplete?: boolean;
    fuse_reason?: string;
    evidence?: Evidence[];  // Agent 模式：最终编号后的证据列表
    verdict?: string;
    confidence?: number;
    acl_filtered?: number;  // 阶段 9：权限过滤数
    retrieval?: import("../types").RetrievalStats | null;
    crag?: import("../types").CragInfo | null;
    refusal_reason?: string;
    regenerated?: boolean;
    unsupported_claims_count?: number;
  }) => void;
  onError: (error: { code: number; message: string; retry_after?: number }) => void;
  // ---- 阶段 6：Agent 轨迹事件 ----
  onPlan?: (data: { steps: AgentPlanStep[]; max_iterations: number; replan?: boolean }) => void;
  onToolStart?: (data: { step_index: number; tool: string; input: string }) => void;
  onToolEnd?: (data: {
    step_index: number; tool: string; elapsed_ms: number;
    hit_count: number; error: string; truncated_from?: number;
    evidence?: Evidence[];  // 该步带回的证据（轨迹↔证据双向定位）
  }) => void;
  onReflect?: (data: { evidence_score: number; next_action: string; reason: string }) => void;
  onIteration?: (data: { current: number; max: number }) => void;
  onFuse?: (data: { reason: string; iteration: number }) => void;
}

const KNOWN_EVENTS = new Set([
  "token", "evidence", "retrieval_stats", "query_analysis",
  "crag_branch", "refine", "confidence", "refused",
  "plan", "tool_start", "tool_end", "reflect", "iteration", "fuse", "replan",
  // 阶段 7 预留：图谱关系路径。缺了它前端会把后端已发出的事件静默丢弃，
  // 表现为"跨模态证据形态不完整"且无处排查。
  "graph_path",
  "done", "error",
]);

export async function askStream(
  question: string,
  signal: AbortSignal,
  handlers: StreamHandlers,
  options: { sessionId?: string | null; mode?: "auto" | "agent" } = {},
): Promise<void> {
  let resp: Response;
  try {
    resp = await fetch("/ask/stream", {
      method: "POST",
      // TASK-160 前端配套：SSE 走 fetch（非 EventSource），因此可以带身份头。
      // 若当初用 EventSource 就无法设置请求头，鉴权改造会在这里卡住。
      headers: withAuth({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        question,
        ...(options.sessionId ? { session_id: options.sessionId } : {}),
        ...(options.mode ? { mode: options.mode } : {}),
      }),
      signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
    handlers.onError({ code: 5001, message: "网络异常，请检查连接后重试" });
    return;
  }
  if (!resp.ok || !resp.body) {
    if (resp.status === 429) {
      // 限流（保护能力）：显式告知 + Retry-After 倒计时
      const retryAfter = Number(resp.headers.get("Retry-After") ?? "5") || 5;
      let message = "请求过于频繁，请稍后重试";
      try {
        const body = await resp.json();
        message = body?.error?.message ?? message;
      } catch {
        /* 忽略非 JSON 响应体 */
      }
      handlers.onError({ code: 6101, message, retry_after: retryAfter });
      return;
    }
    // 鉴权失败必须与"服务挂了"区分：前者改身份即可，后者要等运维
    if (isAuthFailure(resp.status)) {
      handlers.onError({ code: resp.status, message: authErrorMessage(resp.status) });
      return;
    }
    handlers.onError({ code: 5001, message: "服务暂时不可用，请稍后再试" });
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let terminated = false;

  const dispatch = (event: string, data: unknown) => {
    if (!KNOWN_EVENTS.has(event)) return; // 未知事件静默忽略
    if (event === "token") handlers.onToken((data as { content: string }).content);
    else if (event === "evidence") handlers.onEvidence(data as Evidence);
    else if (event === "query_analysis") handlers.onQueryAnalysis(data as QueryAnalysis);
    else if (event === "crag_branch") handlers.onCragBranch(data as { branch: string; avg_score: number; round: number });
    else if (event === "refine") handlers.onRefine(data as { before: string; after: string });
    else if (event === "confidence") handlers.onConfidence(data as ConfidencePayload);
    else if (event === "refused") handlers.onRefused(data as { reason: RefusalReason; trace_id: string; acl_filtered?: number });
    else if (event === "retrieval_stats")
      handlers.onRetrievalStats(data as Partial<RetrievalStats> & { from_cache: boolean; reranked: boolean });
    else if (event === "plan") handlers.onPlan?.(data as { steps: AgentPlanStep[]; max_iterations: number; replan?: boolean });
    else if (event === "tool_start") handlers.onToolStart?.(data as { step_index: number; tool: string; input: string });
    else if (event === "tool_end") handlers.onToolEnd?.(data as { step_index: number; tool: string; elapsed_ms: number; hit_count: number; error: string; truncated_from?: number });
    else if (event === "reflect") handlers.onReflect?.(data as { evidence_score: number; next_action: string; reason: string });
    else if (event === "iteration") handlers.onIteration?.(data as { current: number; max: number });
    else if (event === "fuse") handlers.onFuse?.(data as { reason: string; iteration: number });
    else if (event === "done") {
      terminated = true;
      handlers.onDone(data as { trace_id: string; elapsed_ms: number; from_cache?: boolean; session_id?: string | null; agent?: import("../types").AgentTrace; incomplete?: boolean; fuse_reason?: string });
    } else if (event === "error") {
      terminated = true;
      handlers.onError(data as { code: number; message: string });
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const rawEvent = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        let event = "";
        let data = "";
        for (const line of rawEvent.split("\n")) {
          if (line.startsWith("event: ")) event = line.slice(7).trim();
          else if (line.startsWith("data: ")) data = line.slice(6);
        }
        if (event && data) {
          try {
            dispatch(event, JSON.parse(data));
          } catch {
            /* 单条事件解析失败不阻断后续事件 */
          }
        }
      }
    }
  } catch (err) {
    if ((err as Error).name === "AbortError") return;
  }
  // 流结束但未到终止事件 → 界面不允许永久停留在加载态
  if (!terminated) {
    handlers.onError({ code: 5001, message: "连接中断，请重试" });
  }
}
