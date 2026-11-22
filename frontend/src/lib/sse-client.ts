/** SSE 客户端。
 *
 * 协议纪律：
 * 1. 每类事件有唯一处理入口；
 * 2. 未知事件静默忽略（前后端可独立灰度）；
 * 3. done 与 error 互斥且必有其一；
 * 4. 容忍证据先于文本到达。
 *
 *  增量：retrieval_stats 事件（分阶段耗时）；
 * HTTP 429 时解析 Retry-After 并透传给界面（倒计时 + 自动重试）。
 */
import { CLIENT_TIMEOUT_CODE } from "./api-client";
import {
  backpressureMessage,
  inProgressMessage,
  isInProgressStatus,
  parseRetryAfter,
} from "./retry-after";
import { SseParser } from "./sse-parse";
import type {
  AgentPlanStep,
  AnswerSource,
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
  /** 答案来源：parametric 表示知识库未收录、由模型自身知识回答 */
  answer_source?: AnswerSource;
  /** 被剔除的无据声明原文（前端要告诉用户剔除了哪句） */
  removed_claims?: string[];
  self_check?: import("../types").SelfCheckInfo | null;
  crag?: { branch: string; rounds: { round: number; branch: "correct" | "ambiguous" | "incorrect"; avg_score: number; evidence_count: number; refined_query: string }[] } | null;
}

export interface StreamHandlers {
  onToken: (content: string) => void;
  onEvidence: (evidence: Evidence) => void;
  onRetrievalStats: (stats: Partial<RetrievalStats> & { from_cache: boolean; reranked: boolean }) => void;
  onQueryAnalysis: (analysis: QueryAnalysis) => void;
  onCragBranch: (data: { branch: string; avg_score: number; round: number; }) => void;
  onRefine: (data: { before: string; after: string }) => void;
  onConfidence: (data: ConfidencePayload) => void;
  onRefused: (data: { reason: RefusalReason; trace_id: string; }) => void;
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
    answer_source?: AnswerSource;
        retrieval?: import("../types").RetrievalStats | null;
    crag?: import("../types").CragInfo | null;
    refusal_reason?: string;
    regenerated?: boolean;
    unsupported_claims_count?: number;
  }) => void;
  onError: (error: { code: number; message: string; retry_after?: number }) => void;
  /** 收到心跳（SSE 注释行 `: ping`）。用于重置空闲看门狗之外的保活提示，可省略。 */
  onHeartbeat?: () => void;
  // ---- F5：推荐追问（后端在 done 之前发出）----
  onFollowups?: (data: { items: string[] }) => void;
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
  // F5：推荐追问。**必须登记**：白名单外的 event 会被直接丢弃且不报错，
  // 表现是"后端有发、前端没显示"，且没有任何线索可查。
  "followups",
  "done", "error",
]);

/** 流式空闲上限：后端每 15s 发一条 `: ping` 注释行，4 次都没来就判定连接已死。
 *
 * 没有它的时候，中间代理静默断流（TCP 还在、但没有数据）会让 `reader.read()`
 * **永不 settle**，界面永久停留在加载态，用户既看不到错也拿不到结果（`X-21` 残余）。
 * 60s 是"4 个心跳周期"：够容忍一次网络抖动，又不至于让用户白等几分钟。
 */
const SSE_IDLE_TIMEOUT_MS = 60_000;

/** 断线续传的尝试次数与退避（毫秒）。
 *
 * 两次足够覆盖"代理闪断 / 切后台回来"；再多就是在服务端已经放弃这个 run
 * 之后反复来敲，只增加噪声。退避 300ms → 900ms：第一次要快（用户还在等），
 * 第二次拉开一点给网络恢复的时间。
 */
const RESUME_ATTEMPTS = 2;
const RESUME_BACKOFF_MS = [300, 900];

export async function askStream(
  question: string,
  signal: AbortSignal,
  handlers: StreamHandlers,
  options: { sessionId?: string | null; mode?: "auto" | "agent" } = {},
): Promise<void> {
  // 内部 controller 收拢两条中断来源：调用方主动取消、空闲看门狗。
  // 直接用外部 signal 的话，看门狗只能靠 `Promise.race` 假取消——底层连接还开着。
  const controller = new AbortController();
  let idleExpired = false;
  let idleTimer: ReturnType<typeof setTimeout> | undefined;

  const abort = () => {
    if (!controller.signal.aborted) controller.abort();
  };
  if (signal.aborted) abort();
  else signal.addEventListener("abort", abort, { once: true });

  /** 收到任意字节（含心跳注释行）就重置；覆盖"响应头都还没回来"的那一段 */
  const armIdle = () => {
    if (idleTimer !== undefined) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      idleExpired = true;
      abort();
    }, SSE_IDLE_TIMEOUT_MS);
  };

  let resp: Response;
  try {
    armIdle();
    resp = await fetch("/ask/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        ...(options.sessionId ? { session_id: options.sessionId } : {}),
        ...(options.mode ? { mode: options.mode } : {}),
      }),
      signal: controller.signal,
    });
  } catch (err) {
    if (idleTimer !== undefined) clearTimeout(idleTimer);
    if (idleExpired) {
      handlers.onError({
        code: CLIENT_TIMEOUT_CODE,
        message: `连接超时：${SSE_IDLE_TIMEOUT_MS / 1000} 秒内没有收到任何数据。` +
          "服务可能正在过载或连接已被中断，请重试。",
      });
      return;
    }
    if ((err as Error).name === "AbortError") return;
    handlers.onError({ code: 5001, message: "网络异常，请检查连接后重试" });
    return;
  }
  if (!resp.ok || !resp.body) {
    // 不会进入读循环：先停掉看门狗，否则 60s 后它会在一个已经结束的流上触发 abort
    if (idleTimer !== undefined) clearTimeout(idleTimer);
    if (resp.status === 429 || resp.status === 503) {
      // 限流（429）与背压（503，并发闸门满）都是"稍后来"而不是"服务挂了"：
      // 显式告知 + Retry-After 倒计时，与 5xx 服务区分开。
      // 解析逻辑与 `api-client` **同源**（`lib/retry-after.ts`，`C-06`）：
      // 两处各写一份必然漂移（此前非流式路径就没有这个分支）。
      const retryAfter = parseRetryAfter(resp.headers);
      let message = backpressureMessage(resp.status);
      try {
        const body = await resp.json();
        message = body?.error?.message ?? message;
      } catch {
        /* 忽略非 JSON 响应体 */
      }
      handlers.onError({ code: 6101, message, retry_after: retryAfter });
      return;
    }
    if (isInProgressStatus(resp.status)) {
      // 409「正在处理中」（`MP-03`）：同一问题已有请求在飞，不是故障。
      const retryAfter = parseRetryAfter(resp.headers);
      let message = inProgressMessage();
      try {
        const body = await resp.json();
        message = body?.error?.message ?? message;
      } catch {
        /* 忽略非 JSON 响应体 */
      }
      handlers.onError({ code: 6102, message, retry_after: retryAfter });
      return;
    }
    handlers.onError({ code: 5001, message: "服务暂时不可用，请稍后再试" });
    return;
  }

  const decoder = new TextDecoder();
  // 分帧交给 `sse-parse`（支持多行 `data` 拼接 / `\r\n` / 注释心跳 / `id`），
  // 这里只负责把"字节"翻译成"事件"
  const parser = new SseParser();
  let terminated = false;
  /** 已收到的最后一个事件 id（`{trace_id}:{seq}`）——续传的断点就是它。 */
  let lastEventId: string | undefined;
  /** 服务端是否声明支持续传（`X-SSE-Resume: enabled`）。
   *
   * **只认这一个证据**：没有它去重连等于重发问题（新开一条流、重复计费，
   * 且用户已经看到的前半段会被冲掉）。 */
  const resumeEnabled = (resp.headers.get("x-sse-resume") ?? "").toLowerCase() === "enabled";

  const dispatch = (event: string, data: unknown) => {
    if (!KNOWN_EVENTS.has(event)) return; // 未知事件静默忽略
    if (event === "token") handlers.onToken((data as { content: string }).content);
    else if (event === "evidence") handlers.onEvidence(data as Evidence);
    else if (event === "query_analysis") handlers.onQueryAnalysis(data as QueryAnalysis);
    else if (event === "crag_branch") handlers.onCragBranch(data as { branch: string; avg_score: number; round: number });
    else if (event === "refine") handlers.onRefine(data as { before: string; after: string });
    else if (event === "confidence") handlers.onConfidence(data as ConfidencePayload);
    else if (event === "refused") handlers.onRefused(data as { reason: RefusalReason; trace_id: string; });
    else if (event === "retrieval_stats")
      handlers.onRetrievalStats(data as Partial<RetrievalStats> & { from_cache: boolean; reranked: boolean });
    else if (event === "followups")
      handlers.onFollowups?.(data as { items: string[] });
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

  const consume = (frames: ReturnType<SseParser["feed"]>) => {
    // 心跳是"连接还活着"的唯一证据：注释帧不产生事件，但必须重置看门狗
    if (frames.comments > 0) handlers.onHeartbeat?.();
    for (const frame of frames.frames) {
      if (frame.id) lastEventId = frame.id;
      if (!frame.data) continue;
      try {
        dispatch(frame.event, JSON.parse(frame.data));
      } catch {
        /* 单条事件解析失败不阻断后续事件 */
      }
    }
  };

  /** 读完一条连接的响应体。
   *
   * 返回 `"done"`：正常终止（收到 done/error）或调用方主动取消——都不该重连；
   * 返回 `"retry"`：连接中断且**没有**终止事件——这半截答案不该丢，可尝试续传。
   */
  const pumpResponse = async (response: Response): Promise<"done" | "retry"> => {
    const reader = response.body!.getReader();
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        armIdle(); // 收到任意字节（含 `: ping`）即视为活着
        consume(parser.feed(decoder.decode(value, { stream: true })));
        if (terminated) {
          // 已经拿到终局事件：主动放掉底层连接，别让 reader 挂着
          await reader.cancel().catch(() => undefined);
          return "done";
        }
      }
      // 收尾：最后一条事件可能缺结尾空行，宽容解析而不是丢弃
      consume(parser.flush());
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        // 外部取消（用户点了停止）→ 安静退出；看门狗触发 → 值得重连
        return idleExpired ? "retry" : "done";
      }
      return "retry";
    }
    return terminated ? "done" : "retry";
  };

  let outcome = await pumpResponse(resp);

  // ---- 断点续传（`Last-Event-ID`）----
  // 只在服务端声明支持时尝试，且最多 2 次：网络真断了就没有第 3 次的必要，
  // 也避免在服务端已经放弃这个 run 之后反复来敲。
  for (let attempt = 0; outcome === "retry" && attempt < RESUME_ATTEMPTS; attempt += 1) {
    const traceId = lastEventId ? lastEventId.slice(0, lastEventId.lastIndexOf(":")) : "";
    if (!resumeEnabled || !traceId || controller.signal.aborted) break;
    await new Promise((resolve) => setTimeout(resolve, RESUME_BACKOFF_MS[attempt] ?? 900));
    if (controller.signal.aborted) return;
    idleExpired = false;
    let resumed: Response;
    try {
      armIdle();
      resumed = await fetch(`/ask/stream/${encodeURIComponent(traceId)}`, {
        method: "GET",
        headers: lastEventId ? { "Last-Event-ID": lastEventId } : {},
        signal: controller.signal,
      });
    } catch (err) {
      if (idleExpired) {
        handlers.onError({
          code: CLIENT_TIMEOUT_CODE,
          message: `续传超时：${SSE_IDLE_TIMEOUT_MS / 1000} 秒内没有收到任何数据。`,
        });
        return;
      }
      if ((err as Error).name === "AbortError") return;
      break; // 续传请求本身失败：交给下面统一报"连接中断"
    }
    if (!resumed.ok || !resumed.body) {
      if (idleTimer !== undefined) clearTimeout(idleTimer);
      // 404 = 记录已过期 / 不可续传。明确告诉用户"已收到的可能不完整"，
      // 而不是把它当成普通网络错误（那会让人以为再点一次就能接着看）
      handlers.onError({
        code: 4304,
        message: "断线后无法续传（流式记录已过期），已收到的内容可能不完整，请重新提问。",
      });
      return;
    }
    resp = resumed;
    outcome = await pumpResponse(resumed);
  }

  if (idleTimer !== undefined) clearTimeout(idleTimer);
  if (outcome === "retry") {
    // 流结束但未到终止事件 → 界面不允许永久停留在加载态
    if (controller.signal.aborted && !idleExpired) return;
    if (idleExpired) {
      handlers.onError({
        code: CLIENT_TIMEOUT_CODE,
        message: `连接中断：${SSE_IDLE_TIMEOUT_MS / 1000} 秒内没有收到任何数据（含心跳）。` +
          "结果可能不完整，请重试。",
      });
      return;
    }
    handlers.onError({ code: 5001, message: "连接中断，请重试" });
  }
}
