/** 非流式 API 客户端：统一信封解析。 */
import { authErrorMessage, authHeaders, isAuthFailure, notifyUnauthorized, withAuth } from "./auth";
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
import type {
  AclImpact,
  DocAcl,
  KbGrant,
  KbGrantResult,
  KnowledgeBase,
  LoginResponse,
  MeResponse,
  UserRecord,
} from "../types";

/**
 * 登录/注册端点自身返回 401 是**正常业务结果**（口令错了），
 * 不能触发"清会话 + 跳登录"——那会把"用户名或密码不正确"这条提示吃掉，
 * 用户只看到登录页一闪，压根不知道发生了什么。
 */
const AUTH_ENTRY_PATHS = new Set(["/auth/login", "/auth/register"]);

export async function request<T>(path: string, init?: RequestInit): Promise<Envelope<T>> {
  //  前端配套：所有请求统一注入身份（否则管理页全部 403）。
  // 加载入 authHeaders 而不是内联，是为了保证新增的 API 函数不会漏带凭证。
  const resp = await fetch(path, { ...init, headers: withAuth(init?.headers) });
  if (resp.status === 401 && !AUTH_ENTRY_PATHS.has(path.split("?")[0])) {
    notifyUnauthorized();
  }
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
        message: isAuthFailure(resp.status)
          ? authErrorMessage(resp.status)
          : `请求失败（HTTP ${resp.status}）`,
      },
      meta: { trace_id: "", elapsed_ms: 0 },
    };
  }
  // 401/403 但信封里没有可读原因时补一句——用户需要知道"是权限问题"而非"系统坏了"
  if (isAuthFailure(resp.status) && body && !body.error) {
    return {
      ...body,
      status: "error",
      error: { code: resp.status, message: authErrorMessage(resp.status) },
    } as Envelope<T>;
  }
  return body;
}

export function ask(question: string): Promise<Envelope<AskData>> {
  return request<AskData>("/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
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
  return request("/knowledge/ingest", { method: "POST", body: form });
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
  return request<KnowledgeFile>(`/knowledge/${encodeURIComponent(docId)}/reparse`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chunk_policy: chunkPolicy, parse_policy: parsePolicy }),
  });
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

export function fetchUsers(): Promise<Envelope<UserRecord[]>> {
  return request<UserRecord[]>("/admin/users");
}

export function editUser(userId: string, patch: { roles: string[]; groups: string[]; enabled: boolean }): Promise<Envelope<UserRecord>> {
  return request(`/admin/users/${encodeURIComponent(userId)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
  });
}

/** 管理员为账号设置/重置口令。只有管理员能调（后端挂在 /admin/* 的 admin 门禁下）。 */
export function setUserPassword(userId: string, password: string): Promise<Envelope<{ user_id: string; password_set: boolean }>> {
  return request(`/admin/users/${encodeURIComponent(userId)}/password`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password }),
  });
}

// ---------- 登录 / 注册 / 当前身份 ----------

export function login(username: string, password: string): Promise<Envelope<LoginResponse>> {
  return request<LoginResponse>("/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export function register(username: string, password: string): Promise<Envelope<{ user_id: string; roles: string[] }>> {
  return request<{ user_id: string; roles: string[] }>("/auth/register", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
}

export function fetchMe(): Promise<Envelope<MeResponse>> {
  return request<MeResponse>("/auth/me");
}

export function fetchAclList(): Promise<Envelope<DocAcl[]>> {
  return request<DocAcl[]>("/admin/documents/acl");
}

export function setDocAcl(docId: string, acl: { allow: string[]; deny: string[]; is_public: boolean }): Promise<Envelope<{ impact: AclImpact; risk: string }>> {
  return request(`/admin/documents/${encodeURIComponent(docId)}/acl`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(acl),
  });
}

// ---------- 知识库管理 ----------

export function fetchKnowledgeBases(): Promise<Envelope<KnowledgeBase[]>> {
  return request<KnowledgeBase[]>("/admin/kb");
}

export function createKnowledgeBase(kb: { name: string; description: string; is_public: boolean }): Promise<Envelope<KnowledgeBase>> {
  return request("/admin/kb", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(kb),
  });
}

export function updateKnowledgeBase(kbId: string, patch: { name?: string; description?: string; is_public?: boolean }): Promise<Envelope<KnowledgeBase>> {
  return request(`/admin/kb/${encodeURIComponent(kbId)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
  });
}

export function deleteKnowledgeBase(kbId: string): Promise<Envelope<{ kb_id: string; deleted: boolean }>> {
  return request(`/admin/kb/${encodeURIComponent(kbId)}`, { method: "DELETE" });
}

export function fetchKbGrants(kbId: string): Promise<Envelope<KbGrant[]>> {
  return request<KbGrant[]>(`/admin/kb/${encodeURIComponent(kbId)}/grants`);
}

export function setKbGrants(kbId: string, userIds: string[]): Promise<Envelope<KbGrantResult>> {
  return request(`/admin/kb/${encodeURIComponent(kbId)}/grants`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ user_ids: userIds }),
  });
}

export function fetchUserKbs(userId: string): Promise<Envelope<{ kb_id: string; name: string; is_public: boolean }[]>> {
  return request(`/admin/users/${encodeURIComponent(userId)}/kbs`);
}

/** 单文件上传（XMLHttpRequest 以获得上传进度）。kbId 指定目标知识库，缺省归默认库。 */
export function uploadFile(
  file: File,
  onProgress: (percent: number) => void,
  kbId?: string,
): Promise<Envelope<KnowledgeFile>> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/knowledge/upload");
    // 上传走 XHR（为了进度事件），无法复用 request()，必须单独注入身份头
    Object.entries(authHeaders()).forEach(([k, v]) => xhr.setRequestHeader(k, v));
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
    if (kbId) form.append("kb_id", kbId);
    xhr.send(form);
  });
}

// ---------- F4 快问（单文档「上传即问」）----------

export interface QuickAskFile {
  doc_id: string;
  filename: string;
  chunks: number;
  ttl_s: number;
  expires_in_s?: number;
  session_id?: string;
}

/**
 * 上传文件到快问通道（不入知识库，临时索引 + TTL 到期自动清理）。
 *
 * `sessionId` 给了就把临时文档绑定到该会话（会话内快速解析）：
 * 之后在别的会话里拿这个 doc_id 提问会被拒（404）。
 */
export function uploadQuickAsk(file: File, sessionId?: string | null): Promise<Envelope<QuickAskFile>> {
  const form = new FormData();
  form.append("file", file);
  if (sessionId) form.append("session_id", sessionId);
  return request<QuickAskFile>("/quickask/upload", { method: "POST", body: form });
}

/** 列出某会话下的临时文档（会话内快速解析的恢复入口）。 */
export function listQuickAskDocs(sessionId: string): Promise<
  Envelope<{ doc_id: string; filename: string; chunks: number; created_at: number }[]>
> {
  return request<{ doc_id: string; filename: string; chunks: number; created_at: number }[]>(
    `/quickask/documents?session_id=${encodeURIComponent(sessionId)}`,
  );
}

export function quickAskStatus(docId: string): Promise<Envelope<{ doc_id: string; filename: string; chunks: number; ttl_s: number }>> {
  return request(`/quickask/${encodeURIComponent(docId)}`);
}

export function deleteQuickAsk(docId: string): Promise<Envelope<{ doc_id: string; deleted: boolean }>> {
  return request(`/quickask/${encodeURIComponent(docId)}`, { method: "DELETE" });
}

// ---------- F2 语音（STS token 由后端代持密钥换取）----------

export interface StsToken {
  appid: string;
  token: string;
  cluster?: string;
  duration: number;
}

/** 换取语音识别 STS token（后端持有火山密钥，前端不接触密钥）。 */
export function fetchStsToken(): Promise<Envelope<StsToken>> {
  return request<StsToken>("/speech/sts-token", { method: "POST" });
}

/** 语音能力探测（不消耗 token：用来决定麦克风按钮是否可点）。 */
export function fetchSpeechStatus(): Promise<
  Envelope<{
    available: boolean;
    /** sts = 前端直连火山流式；transcribe = 录音上传后端转写；none = 不可用 */
    mode: "sts" | "transcribe" | "none";
    cluster?: string;
    provider?: string | null;
    model?: string | null;
  }>
> {
  return request<{
    available: boolean;
    mode: "sts" | "transcribe" | "none";
    cluster?: string;
    provider?: string | null;
    model?: string | null;
  }>("/speech/status");
}

/**
 * 上传录音给后端转写。
 *
 * 录音默认是 16kHz/单声道/16bit **裸 PCM**（`audio/pcm`）：Chrome 的
 * MediaRecorder 只能产出 webm/opus，而云端 ASR 普遍不收 webm；PCM 是各端
 * 都稳定支持的中间格式，后端包一层 WAV 头再转发给云厂商。
 */
export function transcribeAudio(
  blob: Blob,
  sampleRate = 16000,
): Promise<Envelope<{ text: string; language?: string | null; provider?: string; model?: string }>> {
  const form = new FormData();
  form.append("file", blob, "audio.pcm");
  form.append("sample_rate", String(sampleRate));
  return request<{ text: string; language?: string | null; provider?: string; model?: string }>(
    "/speech/transcribe",
    { method: "POST", body: form },
  );
}
