/** 非流式 API 客户端：统一信封解析（阶段 0 前端规范 §7.1）。 */
import { authErrorMessage, authHeaders, isAuthFailure, withAuth } from "./auth";
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
  CommunityInfo,
  CritiqueRound,
  EntityDetail,
  EvidenceRecordView,
  GraphEntity,
  GraphStats,
  MediaItem,
  MultiAgentMetrics,
  PlanTask,
  ReportDetail,
  ReportSummary,
  Subgraph,
} from "../types";
import type {
  AclImpact,
  AuditEvent,
  DocAcl,
  EvalDataset,
  EvalRunDetail,
  EvalRunSummary,
  PlatformQuota,
  QuotaRow,
  SensitiveWord,
  UserRecord,
} from "../types";

export async function request<T>(path: string, init?: RequestInit): Promise<Envelope<T>> {
  // TASK-160 前端配套：所有请求统一注入身份（否则管理页全部 403）。
  // 加载入 authHeaders 而不是内联，是为了保证新增的 API 函数不会漏带凭证。
  const resp = await fetch(path, { ...init, headers: withAuth(init?.headers) });
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

// ---------- 阶段 2：管理与观测端点 ----------

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

// ---------- 阶段 3：入库任务与预览 ----------

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

// ---------- 阶段 5：拒答反馈与质量指标 ----------

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

// ---------- 阶段 6：会话与 Agent 指标 ----------

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

// ---------- 阶段 7：图谱与多模态 ----------

export function fetchGraphEntities(q = "", types = ""): Promise<Envelope<GraphEntity[]>> {
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (types) params.set("types", types);
  return request<GraphEntity[]>(`/graph/entities?${params}`);
}

export function fetchEntityDetail(name: string): Promise<Envelope<EntityDetail>> {
  return request<EntityDetail>(`/graph/entity/${encodeURIComponent(name)}`);
}

export function fetchSubgraph(seed = "", depth = 1): Promise<Envelope<Subgraph>> {
  const params = new URLSearchParams();
  if (seed) params.set("seed", seed);
  params.set("depth", String(depth));
  return request<Subgraph>(`/graph/subgraph?${params}`);
}

export function fetchCommunities(): Promise<Envelope<CommunityInfo[]>> {
  return request<CommunityInfo[]>("/graph/communities");
}

export function fetchGraphStats(): Promise<Envelope<GraphStats>> {
  return request<GraphStats>("/graph/stats");
}

export function buildGraph(docId?: string): Promise<Envelope<{ chunks_processed: number; entities_upserted: number; relations_upserted: number }>> {
  return request("/graph/build", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ doc_id: docId ?? null, rebuild_communities: true }),
  });
}

export function searchMedia(query: string, topK = 4): Promise<Envelope<{ items: MediaItem[]; total: number }>> {
  return request("/search/media", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, top_k: topK }),
  });
}

// ---------- 阶段 8：报告中心 ----------

export function createReport(goal: string): Promise<Envelope<{ report_id: string; title: string; status: string }>> {
  return request("/reports", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ goal }),
  });
}

export function listReports(): Promise<Envelope<ReportSummary[]>> {
  return request<ReportSummary[]>("/reports");
}

export function getReport(reportId: string): Promise<Envelope<ReportDetail>> {
  return request<ReportDetail>(`/reports/${encodeURIComponent(reportId)}`);
}

export function getReportPlan(reportId: string): Promise<Envelope<{ tasks: PlanTask[]; layers: number }>> {
  return request(`/reports/${encodeURIComponent(reportId)}/plan`);
}

export function getReportEvidence(reportId: string): Promise<Envelope<{ records: EvidenceRecordView[]; total: number; adopted: number; rejected: number }>> {
  return request(`/reports/${encodeURIComponent(reportId)}/evidence`);
}

export function getReportCritique(reportId: string): Promise<Envelope<{ rounds: CritiqueRound[]; final_score: number | null; accepted: boolean | null; unresolved_issues: string[] }>> {
  return request(`/reports/${encodeURIComponent(reportId)}/critique`);
}

export function deleteReport(reportId: string): Promise<Envelope<{ deleted: boolean }>> {
  return request(`/reports/${encodeURIComponent(reportId)}`, { method: "DELETE" });
}

export function fetchMultiAgentMetrics(): Promise<Envelope<MultiAgentMetrics>> {
  return request<MultiAgentMetrics>("/admin/metrics/multiagent");
}

// ---------- 阶段 9：企业护栏 ----------

export function fetchUsers(): Promise<Envelope<UserRecord[]>> {
  return request<UserRecord[]>("/admin/users");
}

export function editUser(userId: string, patch: { roles: string[]; groups: string[]; enabled: boolean }): Promise<Envelope<UserRecord>> {
  return request(`/admin/users/${encodeURIComponent(userId)}`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
  });
}

export function fetchAclList(): Promise<Envelope<DocAcl[]>> {
  return request<DocAcl[]>("/admin/documents/acl");
}

export function setDocAcl(docId: string, acl: { allow: string[]; deny: string[]; is_public: boolean }): Promise<Envelope<{ impact: AclImpact; risk: string }>> {
  return request(`/admin/documents/${encodeURIComponent(docId)}/acl`, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(acl),
  });
}

export function fetchAudit(params: { range_s?: number; event?: string; user_id?: string; trace_id?: string }): Promise<Envelope<AuditEvent[]>> {
  const q = new URLSearchParams();
  if (params.range_s) q.set("range_s", String(params.range_s));
  if (params.event) q.set("event", params.event);
  if (params.user_id) q.set("user_id", params.user_id);
  if (params.trace_id) q.set("trace_id", params.trace_id);
  return request<AuditEvent[]>(`/admin/audit?${q}`);
}

export function fetchQuota(): Promise<Envelope<QuotaRow[]>> {
  return request<QuotaRow[]>("/admin/quota");
}

export function setQuota(tenantId: string, perMinute: number, perDay: number): Promise<Envelope<unknown>> {
  return request("/admin/quota", {
    method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tenant_id: tenantId, per_minute: perMinute, per_day: perDay }),
  });
}

export function fetchPlatformQuota(): Promise<Envelope<PlatformQuota>> {
  return request<PlatformQuota>("/admin/quota/platform");
}

export function fetchSensitiveWords(tenantId?: string): Promise<Envelope<SensitiveWord[]>> {
  return request<SensitiveWord[]>(`/admin/sensitive${tenantId ? `?tenant_id=${tenantId}` : ""}`);
}

export function addSensitiveWord(word: SensitiveWord): Promise<Envelope<unknown>> {
  return request("/admin/sensitive", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(word),
  });
}

export function deleteSensitiveWord(word: string, tenantId = "default"): Promise<Envelope<unknown>> {
  return request(`/admin/sensitive/${encodeURIComponent(word)}?tenant_id=${tenantId}`, { method: "DELETE" });
}

export function fetchEvalDatasets(): Promise<Envelope<EvalDataset[]>> {
  return request<EvalDataset[]>("/admin/eval/datasets");
}

export function saveEvalDataset(name: string, version: string, samples: Record<string, unknown>[]): Promise<Envelope<unknown>> {
  return request("/admin/eval/datasets", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, version, samples }),
  });
}

export function runEval(dataset: string, datasetVersion: string): Promise<Envelope<EvalRunSummary>> {
  return request("/admin/eval/run", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dataset, dataset_version: datasetVersion }),
  });
}

export function fetchEvalRuns(): Promise<Envelope<EvalRunSummary[]>> {
  return request<EvalRunSummary[]>("/admin/eval/runs");
}

export function fetchEvalRun(runId: string): Promise<Envelope<EvalRunDetail>> {
  return request<EvalRunDetail>(`/admin/eval/runs/${encodeURIComponent(runId)}`);
}

/** 单文件上传（XMLHttpRequest 以获得上传进度）。 */
export function uploadFile(
  file: File,
  onProgress: (percent: number) => void,
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
    xhr.send(form);
  });
}
