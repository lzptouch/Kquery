/** 非流式 API 客户端：统一信封解析。
 *
 * 认证与授权已下线（2026-11-12）：请求不再注入任何身份头，也不再有
 * "401 → 清会话 + 跳登录"的全局处理——后端不再返回 401（凭证校验已移除）。
 *
 * ## 请求超时（`X-21` / `X-26`）
 *
 * 此前 `request()` 直接 `await fetch(...)`——**没有任何超时**。后果不是"等久一点"：
 * 后端进程挂了但 TCP 还开着（或中间网关静默丢包）时，这个 Promise **永不 settle**，
 * 页面上的 loading 会一直转，用户只能刷新；而刷新又会因为幂等在飞（409）拿不到结果。
 *
 * 三条纪律：
 * 1. **前端的超时窗口必须长于后端的 deadline**，否则前端先断开、后端还在算并烧 token，
 *    用户却什么也看不到。所以 `/ask` 用 `ASK_TIMEOUT_MS`（130s）> `ask_deadline_ms`（120s）。
 * 2. 超时**返回错误信封**而不是 throw：与「非 JSON 响应」分支同风格，调用方不需要额外 catch，
 *    也不会产生未捕获的 Promise 拒绝。
 * 3. 超时 ≠ 用户主动取消：两者都走 `AbortController`，但只有定时器触发时 `timedOut` 为真，
 *    文案必须分开——把"用户关掉了"说成"服务超时"会误导排障。
 */
import type {
  AgentMetrics,
  AskData,
  ChunkDetail,
  DocPreview,
  Envelope,
  FeedbackTicket,
  KnowledgeFile,
  KnowledgeStats,
  MetricsSummary,
  ProbeResult,
  QualityMetrics,
  RetrievalConfig,
  RetrievalConfigView,
  ScopedConfig,
  SessionDetail,
  SessionSummary,
  TaskView,
  TimeseriesPoint,
  TopQuery,
} from "../types";
import type {
  GraphStats,
  MediaItem,
} from "../types";
import {
  backpressureMessage,
  inProgressMessage,
  isBackpressureStatus,
  isInProgressStatus,
  parseRetryAfter,
} from "./retry-after";
/** 管理 / 观测接口的默认超时：这些端点只读本地状态，30s 不返回就是出问题了。 */
export const DEFAULT_TIMEOUT_MS = 30_000;
/** 非流式问答：必须长于后端 `ask_deadline_ms`（默认 120s），见文件头纪律 1。 */
export const ASK_TIMEOUT_MS = 130_000;
/** 上传：耗时由体积决定，给足 120s。 */
export const UPLOAD_TIMEOUT_MS = 120_000;

/** 前端本地错误码（9xxx）。后端**永不**返回这一段，见 `docs` 与 `envelope.py` 分段说明。 */
export const CLIENT_TIMEOUT_CODE = 9101;
export const CLIENT_NETWORK_ERROR_CODE = 9102;
export const CLIENT_CANCELLED_CODE = 9103;

export interface RequestOptions {
  /** 覆盖默认超时；`0` = 不设超时（由调用方自己管取消）。 */
  timeoutMs?: number;
}

function clientFailureEnvelope<T>(
  code: number,
  message: string,
  elapsedMs: number,
): Envelope<T> {
  return {
    status: "error",
    data: null,
    error: { code, message },
    meta: { trace_id: "", elapsed_ms: elapsedMs },
  };
}

export async function request<T>(
  path: string,
  init?: RequestInit,
  options: RequestOptions = {},
): Promise<Envelope<T>> {
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const startedAt = Date.now();
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let timedOut = false;

  if (timeoutMs > 0) {
    timer = setTimeout(() => {
      // 先记原因再 abort：abort 事件是同步派发的，顺序反了就拿不到 "为什么断"
      timedOut = true;
      controller.abort();
    }, timeoutMs);
  }
  // 外部 signal（调用方主动取消）也接到内部 controller 上
  const external = init?.signal;
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener("abort", () => controller.abort(), { once: true });
  }

  let resp: Response;
  try {
    resp = await fetch(path, { ...init, signal: controller.signal });
  } catch {
    // 超时、主动取消、网络异常都落在这里（fetch 只给一个 AbortError / TypeError，不给原因），
    // 所以原因必须靠我们自己的标记区分——三者对用户的含义完全不同
    const elapsed = Date.now() - startedAt;
    if (timedOut) {
      return clientFailureEnvelope<T>(
        CLIENT_TIMEOUT_CODE,
        `请求超时：${Math.round(timeoutMs / 1000)} 秒内没有响应。` +
          "服务可能正在过载或网络已中断，请重试；反复超时请联系管理员。",
        elapsed,
      );
    }
    if (external?.aborted) {
      return clientFailureEnvelope<T>(CLIENT_CANCELLED_CODE, "请求已取消。", elapsed);
    }
    return clientFailureEnvelope<T>(
      CLIENT_NETWORK_ERROR_CODE,
      "网络异常：无法连接到服务。请检查网络或确认后端已启动。",
      elapsed,
    );
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
  // 背压提示（`C-06`）：429 / 503 是「稍后再来」，不是故障——
  // `Retry-After` 在**响应头**里（不在信封里），所以必须在解析 body 之前取。
  const backpressure = isBackpressureStatus(resp.status);
  // 409「正在处理中」（`MP-03`）：在飞去重，不是故障——等前一个请求出结果才是正解。
  const inProgress = isInProgressStatus(resp.status);
  const retryAfter =
    backpressure || inProgress ? parseRetryAfter(resp.headers) : undefined;

  let body: Envelope<T>;
  try {
    body = (await resp.json()) as Envelope<T>;
  } catch {
    // 非 JSON 响应（网关 502、nginx 502 HTML 等）：不能直接 JSON.parse 崩掉，
    // 否则"后端没起来"会表现成一个无提示的白屏
    const code = resp.status || 5001;
    return {
      status: "error",
      data: null,
      error: {
        code,
        message: backpressure
          ? backpressureMessage(resp.status)
          : inProgress
            ? inProgressMessage()
            : `请求失败（HTTP ${resp.status}）`,
        // 背压才带可重试秒数；500 等真故障**不带**——
        // 把「服务挂了」伪装成「稍后再试」会让用户一直等一个不会自愈的失败。
        ...(retryAfter !== undefined ? { retry_after: retryAfter } : {}),
      },
      meta: { trace_id: "", elapsed_ms: 0 },
    };
  }

  if (backpressure || inProgress) {
    // 信封里可能没有 message（网关返回的非统一格式）→ 补一条可读的；
    // 同时把 `retry_after` 挂到 error 上，供调用方展示倒计时。
    return {
      ...body,
      status: "error",
      data: body.data ?? null,
      error: {
        code: body.error?.code ?? (inProgress ? 6102 : 6101),
        message:
          body.error?.message ??
          (inProgress ? inProgressMessage() : backpressureMessage(resp.status)),
        retry_after: retryAfter,
      },
    };
  }
  return body;
}

export function ask(question: string): Promise<Envelope<AskData>> {
  return request<AskData>(
    "/ask",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    },
    { timeoutMs: ASK_TIMEOUT_MS },
  );
}

export function listFiles(): Promise<Envelope<KnowledgeFile[]>> {
  return request<KnowledgeFile[]>("/knowledge/files");
}

export function rebuildFile(docId: string): Promise<Envelope<KnowledgeFile>> {
  return request<KnowledgeFile>(`/knowledge/rebuild/${encodeURIComponent(docId)}`, {
    method: "POST",
  });
}

export function deleteFile(docId: string): Promise<Envelope<KnowledgeFile>> {
  return request<KnowledgeFile>(`/knowledge/files/${encodeURIComponent(docId)}`, {
    method: "DELETE",
  });
}

// ---------- 管理与观测端点 ----------

export function fetchMetricsSummary(rangeS = 3600): Promise<Envelope<MetricsSummary>> {
  return request<MetricsSummary>(`/admin/metrics/summary?range_s=${rangeS}`);
}

export function fetchTimeseries(
  metric: string,
  rangeS = 3600,
): Promise<Envelope<{ metric: string; range_s: number; points: TimeseriesPoint[] }>> {
  return request(`/admin/metrics/timeseries?metric=${encodeURIComponent(metric)}&range_s=${rangeS}`);
}

export function fetchTopQueries(limit = 10): Promise<Envelope<TopQuery[]>> {
  return request<TopQuery[]>(`/admin/metrics/top-queries?limit=${limit}`);
}

export function fetchRetrievalConfig(): Promise<Envelope<RetrievalConfigView>> {
  return request<RetrievalConfigView>("/admin/retrieval-config");
}

/** 灰度覆盖作用域列表（`MP-02`：配置页的「实验分组」视图数据源）。 */
export function fetchScopedConfigs(): Promise<Envelope<{ scoped: ScopedConfig[] }>> {
  return request<{ scoped: ScopedConfig[] }>("/admin/retrieval-config/scoped");
}

export function saveRetrievalConfig(
  config: RetrievalConfig,
): Promise<Envelope<{ effective_at: number; message: string }>> {
  return request("/admin/retrieval-config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
}

export function resetRetrievalConfig(): Promise<Envelope<{ effective_at: number; message: string }>> {
  return request("/admin/retrieval-config/reset", { method: "POST" });
}

export function fetchKnowledgeStats(): Promise<Envelope<KnowledgeStats>> {
  return request<KnowledgeStats>("/knowledge/stats");
}

export function probeDocument(docId: string, query: string, topK = 4): Promise<Envelope<ProbeResult>> {
  return request<ProbeResult>(`/knowledge/${encodeURIComponent(docId)}/probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k: topK }),
  });
}

// ---------- 入库任务与预览 ----------

export function ingestTask(
  files: File[],
  chunkPolicy: unknown,
  parsePolicy: unknown,
): Promise<Envelope<{ task_id: string; total_files: number }>> {
  const form = new FormData();
  files.forEach((f) => form.append("files", f));
  form.append("chunk_policy", JSON.stringify(chunkPolicy));
  form.append("parse_policy", JSON.stringify(parsePolicy));
  // 多文件上传：体积决定耗时，不能用 30s 的默认值
  return request(
    "/knowledge/ingest",
    { method: "POST", body: form },
    { timeoutMs: UPLOAD_TIMEOUT_MS },
  );
}

export function fetchTask(taskId: string): Promise<Envelope<TaskView>> {
  return request<TaskView>(`/knowledge/tasks/${encodeURIComponent(taskId)}`);
}

export function retryTask(
  taskId: string,
  fileId?: string,
): Promise<Envelope<{ retried_files: string[]; cleared_products: boolean; reused_parse_products: string[] }>> {
  return request(`/knowledge/tasks/${encodeURIComponent(taskId)}/retry`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file_id: fileId ?? null }),
  });
}

export function cancelTask(taskId: string): Promise<Envelope<{ cancelled_scope: string }>> {
  return request(`/knowledge/tasks/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
}

export function fetchPreview(docId: string): Promise<Envelope<DocPreview>> {
  return request<DocPreview>(`/knowledge/preview/${encodeURIComponent(docId)}`);
}

export function fetchChunkDetail(docId: string, chunkId: string): Promise<Envelope<ChunkDetail>> {
  return request<ChunkDetail>(
    `/knowledge/preview/${encodeURIComponent(docId)}/chunk/${encodeURIComponent(chunkId)}`,
  );
}

export function reparseDocument(
  docId: string,
  chunkPolicy: unknown,
  parsePolicy: unknown = {},
): Promise<Envelope<KnowledgeFile>> {
  return request<KnowledgeFile>(
    `/knowledge/${encodeURIComponent(docId)}/reparse`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chunk_policy: chunkPolicy, parse_policy: parsePolicy }),
    },
    // 重解析是同步链路（解析 + 切块 + 嵌入 + 建索引），30s 是合理的等待而非超时
    { timeoutMs: UPLOAD_TIMEOUT_MS },
  );
}

export function offlineDocument(docId: string): Promise<Envelope<KnowledgeFile>> {
  return request<KnowledgeFile>(`/knowledge/${encodeURIComponent(docId)}/offline`, { method: "POST" });
}

// ---------- 拒答反馈与质量指标 ----------

export function fetchQualityMetrics(): Promise<Envelope<QualityMetrics>> {
  return request<QualityMetrics>("/admin/metrics/quality");
}

export function submitRefusalFeedback(payload: {
  feedback_type: string;
  refusal_reason: string;
  user_note: string;
  trace_id: string;
}): Promise<Envelope<{ ticket_id: string; status: string }>> {
  return request("/feedback/refusal", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function fetchFeedback(ticketId: string): Promise<Envelope<FeedbackTicket>> {
  return request<FeedbackTicket>(`/feedback/${encodeURIComponent(ticketId)}`);
}

// ---------- 会话与 Agent 指标 ----------

export function listSessions(): Promise<Envelope<SessionSummary[]>> {
  return request<SessionSummary[]>("/sessions");
}

export function createSession(title = "新对话"): Promise<Envelope<{ session_id: string; title: string }>> {
  return request("/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export function getSession(sessionId: string): Promise<Envelope<SessionDetail>> {
  return request<SessionDetail>(`/sessions/${encodeURIComponent(sessionId)}`);
}

export function renameSession(sessionId: string, title: string): Promise<Envelope<{ session_id: string; title: string }>> {
  return request(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
}

export function deleteSession(sessionId: string): Promise<Envelope<{ deleted: boolean }>> {
  return request(`/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export function fetchAgentMetrics(): Promise<Envelope<AgentMetrics>> {
  return request<AgentMetrics>("/admin/metrics/agent");
}

// ---------- 多模态 ----------

/** 图谱统计（仅取图片库规模等指标；图谱可视化已下线，后端图谱能力保留）。 */
export function fetchGraphStats(): Promise<Envelope<GraphStats>> {
  return request<GraphStats>("/graph/stats");
}

export function searchMedia(query: string, topK = 4): Promise<Envelope<{ items: MediaItem[]; total: number }>> {
  return request("/search/media", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k: topK }),
  });
}

// ---------- 企业护栏 ----------








/** 单文件上传（XMLHttpRequest 以获得上传进度）。 */
export function uploadFile(
  file: File,
  onProgress: (percent: number) => void,
): Promise<Envelope<KnowledgeFile>> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/knowledge/upload");
    // XHR 不走 `request()`，超时得单独设：否则大文件上传中断时 onload / onerror 都不触发
    xhr.timeout = UPLOAD_TIMEOUT_MS;
    xhr.ontimeout = () =>
      reject(new Error(`上传超时（超过 ${Math.round(UPLOAD_TIMEOUT_MS / 1000)} 秒）`));
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      try {
        resolve(JSON.parse(xhr.responseText) as Envelope<KnowledgeFile>);
      } catch {
        reject(new Error("响应解析失败"));
      }
    };
    xhr.onerror = () => reject(new Error("网络异常"));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}
