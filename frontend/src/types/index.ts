/** 与后端 API 契约对应的类型定义（阶段 1 前端规范 §4）。 */

export interface Evidence {
  rank: number;
  chunk_id: string;
  score?: number | null;
  content: string;
  source: string;
  doc_id: string;
  filename: string;
  /** keyword（关键词路）/ vector（语义路）/ both（两路融合命中）/ graph / image（阶段 7） */
  source_route?: "keyword" | "vector" | "both" | "graph" | "image" | "tool";
  modality?: string;
  chunker?: string;
  /** 阶段 4：该证据由哪条改写/子问题带回 */
  origin?: { kind: string; text: string } | null;
  /** 阶段 5：是否被采纳进生成上下文（未采纳也返回） */
  used?: boolean;
  rejected_reason?: string;
  /** 阶段 7：图像证据资源地址 / 图谱关系支撑块 */
  uri?: string;
  source_chunks?: string[];
}

// ---------- 阶段 7：GraphRAG 与多模态 ----------

export interface GraphEntity {
  name: string;
  type: "Person" | "Org" | "Product" | "Concept" | "Event";
  description: string;
  degree: number;
  community_id: string;
  aliases: string[];
}

export interface GraphNode {
  id: string;
  type: string;
  degree: number;
}

export interface GraphLink {
  source: string;
  target: string;
  relation: string;
  direction: "forward" | "backward";
  weight: number;
}

export interface Subgraph {
  nodes: GraphNode[];
  links: GraphLink[];
  truncated: boolean;
}

export interface EntityDetail extends GraphEntity {
  community: { community_id: string; summary: string; size: number } | null;
  in_edges: { from: string; relation: string; description: string }[];
  out_edges: { to: string; relation: string; description: string }[];
}

export interface CommunityInfo {
  community_id: string;
  size: number;
  summary: string;
  members: string[];
  generated_at: number;
  model_version: string;
}

export interface GraphStats {
  entities: number;
  relations: number;
  communities: number;
  orphan_ratio: number;
  images: number;
  caption_model: string;
}

export interface MediaItem {
  asset_id: string;
  caption: string;
  uri: string;
  page: number;
  doc_id: string;
  score: number;
}

// ---------- 阶段 5：CRAG 与答案判定 ----------

export type Verdict = "answered" | "refused";
export type RefusalReason = "no_content" | "insufficient_evidence" | "permission_denied" | "";

export interface CragRound {
  round: number;
  branch: "correct" | "ambiguous" | "incorrect";
  avg_score: number;
  evidence_count: number;
  refined_query: string;
}

export interface CragInfo {
  branch: string;
  rounds: CragRound[];
}

export interface QualityMetrics {
  faithfulness: number | null;
  refusal_rate: number;
  avg_regeneration: number;
  crag_branch_ratio: Record<string, number>;
  answered: number;
  refused: number;
}

export interface FeedbackTicket {
  ticket_id: string;
  feedback_type: string;
  refusal_reason: string;
  user_note: string;
  trace_id: string;
  status: string;
  created_at: string;
  handled_at: string;
  resolution: string;
}

// ---------- 阶段 4：Query 预处理 ----------

export interface Rewrite {
  type: "original" | "rewrite" | "hyde" | "stepback";
  text: string;
  expander: string;
  used: boolean;
  skip_reason: string;
}

export interface SubQuestion {
  text: string;
  expected_source: string;
  skipped: boolean;
  skip_reason: string;
}

export interface QueryAnalysis {
  analysis_id: string;
  raw_query: string;
  tenant_id: string;
  intent: string;
  intent_confidence: number;
  intent_reason: string;
  needs_retrieval: boolean;
  rewrites: Rewrite[];
  sub_questions: SubQuestion[];
  sources: string[];
  degraded: string[];
  elapsed_ms: number;
  llm_calls: number;
  /** 预估费用（元）：调参时"关掉 HyDE 省了多少"的答案 */
  cost?: number;
  strategy_version: string;
  created_at: string;
}

export interface AnalysisRow {
  analysis_id: string;
  created_at: string;
  raw_query: string;
  tenant_id: string;
  intent: string;
  needs_retrieval: boolean;
  rewrite_count: number;
  sub_question_count: number;
  elapsed_ms: number;
  degraded: string[];
}

export interface QueryPattern {
  intent: string;
  count: number;
  ratio: number;
  avg_elapsed_ms: number;
}

/** 检索统计（阶段 2）：四个计数 + 四段耗时。
 *
 * 后端同时下发平铺字段（内部口径）与嵌套对象（前端规范口径）：
 * `latency_ms` / `candidates` 是文档要求的前端消费形态。
 */
export interface RetrievalStats {
  keyword_candidates: number;
  vector_candidates: number;
  fused_count: number;
  reranked_count: number;
  into_context_count: number;
  keyword_ms: number;
  vector_ms: number;
  fusion_ms: number;
  rerank_ms: number;
  /** 嵌套口径：分阶段耗时 */
  latency_ms?: {
    keyword: number;
    vector: number;
    fusion: number;
    rerank: number;
  };
  /** 嵌套口径：候选漏斗 召回→融合→重排→入上下文 */
  candidates?: {
    keyword: number;
    vector: number;
    fused: number;
    reranked: number;
    into_context: number;
  };
  /** Agent 路径额外给出：工具调用数与其中失败数 */
  tool_calls?: number;
  tool_errors?: number;
  /** 因 ACL 被过滤的条数 */
  acl_filtered?: number;
  from_cache?: boolean;
  reranked_flag?: boolean;
}

/** 幻觉自检明细（阶段 5 §8.2 SELF_CHECK 实体） */
export interface SelfCheckInfo {
  method: string;
  checked_claims: number;
  removed_claims: number;
  passed: boolean;
  unsupported_claims: string[];
  reasoning: string;
  model_version: string;
  elapsed_ms: number;
}

export interface AskData {
  answer: string;
  evidence: Evidence[];
  trace_id: string;
  elapsed_ms: number;
  from_cache: boolean;
  reranked: boolean;
  retrieval: RetrievalStats | null;
  analysis: Record<string, unknown> | null;
  verdict: Verdict;
  refusal_reason: RefusalReason;
  confidence: number | null;
  regenerated: boolean;
  unsupported_claims_count: number;
  crag: CragInfo | null;
}

export interface KnowledgeFile {
  file_id: string;
  filename: string;
  doc_id: string;
  size: number;
  uploaded_at: string;
  chunks: number;
  status: "uploaded" | "parsing" | "indexed" | "failed";
  error?: string | null;
}

export interface Envelope<T> {
  status: "ok" | "warn" | "error";
  data: T | null;
  error: { code: number; message: string } | null;
  meta: { trace_id: string; elapsed_ms: number };
}

/** 当前身份（TASK-160 前端配套）。token 非空时后端只认令牌。 */
export interface Principal {
  user_id: string;
  tenant_id: string;
  roles: string;
  token: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  evidence: Evidence[];
  streaming?: boolean;
  error?: { code: number; message: string; retry_after?: number } | null;
  traceId?: string;
  fromCache?: boolean;
  reranked?: boolean;
  retrieval?: RetrievalStats | null;
  analysis?: QueryAnalysis | null;
  verdict?: Verdict;
  refusalReason?: RefusalReason;
  confidence?: number | null;
  regenerated?: boolean;
  unsupportedClaimsCount?: number;
  crag?: CragInfo | null;
  /** 阶段 5：本次未做答案校验（评分器/自检器降级），置信度应显示为"未知" */
  unvalidated?: boolean;
  /** 被剔除的无据声明原文（前端要告诉用户剔除了哪句） */
  removedClaims?: string[];
  /** CRAG 精炼前后对比（stage-5 §三） */
  refineFrom?: string;
  refineTo?: string;
  // 阶段 6：Agent 轨迹
  agent?: AgentTrace | null;
  traceNodes?: TraceNode[]; // 流式期间的实时轨迹
  incomplete?: boolean;
  fuseReason?: string;
  /** 阶段 9：因权限被过滤的结果数（只报数量，绝不展示被过滤文档的元信息） */
  aclFiltered?: number;
}

// ---------- 阶段 6：Agentic RAG ----------

export interface AgentPlanStep {
  step_index: number;
  description: string;
  tool_name: string;
  depends_on: number[];
  status: "pending" | "done" | "failed";
  result_count: number;
}

export interface AgentToolCall {
  step_index: number;
  tool_name: string;
  arguments: { query: string };
  result_count: number;
  elapsed_ms: number;
  error: string;
  truncated_from?: number;
}

export interface AgentTrace {
  plan: AgentPlanStep[];
  tool_calls: AgentToolCall[];
  tool_errors: { step_index: number; tool: string; error: string }[];
  iterations: number;
  max_iterations: number;
  status: "running" | "completed" | "circuit_breaker" | "failed";
  incomplete: boolean;
  fuse_reason: string;
  degraded: boolean;
}

/** 执行中的轨迹节点（流式渲染用） */
export interface TraceNode {
  kind: "plan" | "tool" | "reflect" | "answer" | "replan";
  stepIndex?: number;
  title: string;
  detail?: string;
  tool?: string;
  input?: string;
  elapsedMs?: number;
  hitCount?: number;
  score?: number;
  nextAction?: string;
  error?: string;
  running?: boolean;
}

export interface SessionSummary {
  session_id: string;
  title: string;
  message_count: number;
  updated_at: number;
}

export interface SessionDetail {
  session_id: string;
  title: string;
  turns: { question: string; answer: string; verdict: string; turn: number }[];
  updated_at: number;
}

export interface AgentMetrics {
  runs: number;
  avg_iterations: number;
  fuse_rate: number;
  tool_distribution: Record<string, number>;
  abnormal: { ts: number; session_id: string; kind: string; detail: string }[];
}

// ---------- 阶段 8：多智能体报告 ----------

/** 报告九态：未通过（rejected/capped）与未评审（unreviewed）严格区分 */
export type ReportStatus =
  | "planning" | "researching" | "validating" | "writing" | "critiquing"
  | "accepted" | "rejected" | "capped" | "unreviewed" | "failed";

export interface ReportSummary {
  report_id: string;
  title: string;
  status: ReportStatus;
  accepted: boolean | null;
  critic_score: number | null;
  rounds: number;
  layers: number;
  created_at: number;
  updated_at: number;
}

export interface PlanTask {
  task_id: string;
  description: string;
  tool_name: string;
  depends_on: string[];
  layer: number;
  status: "pending" | "running" | "done" | "failed";
  failure_reason: string;
}

export interface EvidenceRecordView {
  evidence_id: string;
  task_id: string;
  content: string;
  tool_name: string;
  score: number | null;
  adopted: boolean | null;
  rejected_reason: string;
  used_by_tasks: string[];
  created_at: number;
  chunk_id?: string;
  uri?: string;
  modality?: string;
}

export interface CritiqueRound {
  round: number;
  score: number;
  issues: string[];
  resolved_issues: string[];
  raw_output: string;
  created_at: number;
}

export interface Draft {
  round: number;
  content: string;
  word_count: number;
  created_at: number;
}

export interface ReportDetail extends ReportSummary {
  goal: string;
  role_calls: Record<string, number>;
  unresolved_issues: string[];
  drafts: Draft[];
  blackboard: {
    plan: PlanTask[];
    evidence: EvidenceRecordView[];
    critiques: CritiqueRound[];
  } | null;
  error: string;
}

export interface MultiAgentMetrics {
  reports: number;
  by_status: Record<string, number>;
  avg_subtasks: number;
  avg_critic_rounds: number;
  critic_first_pass_rate: number | null;
  layer_distribution: Record<string, number>;
  role_calls: Record<string, number>;
}

// ---------- 阶段 9：企业护栏 ----------

export interface UserRecord {
  user_id: string;
  tenant_id: string;
  roles: string[];
  groups: string[];
  enabled: boolean;
}

export interface DocAcl {
  doc_id: string;
  filename: string;
  tenant_id: string;
  allow: string[];
  deny: string[];
  is_public: boolean | null;
  effective_public: boolean;
  updated_at?: number;
  updated_by?: string;
}

export interface AclImpact {
  added_count: number;
  removed_count: number;
  added_users: string[];
  removed_users: string[];
  total_after: number;
}

export interface AuditEvent {
  event_id: string;
  trace_id: string;
  event: string;
  tenant_id: string;
  user_id: string;
  payload: Record<string, unknown>;
  created_at: number;
}

export interface QuotaRow {
  tenant_id: string;
  limit_per_minute: number;
  limit_per_day: number;
  current_window: number;
  today_count: number;
  status: "ok" | "warning" | "exceeded";
}

/** 平台侧配额（账号级上游约束，全租户共享）。 */
export interface PlatformQuota {
  budget_per_day: number;
  used_today: number;
  remaining: number | null;
  ratio: number;
  status: "disabled" | "ok" | "warning" | "exhausted";
  day: string;
  models: Record<string, number>;
  platform_errors: {
    rate_limited: number;
    quota: number;
    timeout: number;
    other: number;
  };
}

export interface SensitiveWord {
  word: string;
  category: string;
  action: "mask" | "refuse" | "tag";
  tenant_id: string;
  enabled: boolean;
}

export interface EvalDataset {
  name: string;
  version: string;
  count: number;
}

export interface EvalRunSummary {
  run_id: string;
  dataset: string;
  dataset_version: string;
  model_version: string;
  /** 业务代码版本：指标变化到底是平台换版还是我们改了代码，靠它区分 */
  code_version?: string | null;
  /** 评测走的链路（linear / agent）：两种口径的 faithfulness 不可比 */
  path?: string | null;
  sample_count: number;
  faithfulness: number;
  citation_accuracy: number;
  refusal_accuracy: number | null;
  /** 期望证据被答案真正引用的比例：唯一能区分"检索没找到"与"生成写错了"的指标 */
  coverage?: number | null;
  created_at: number;
}

export interface EvalRunDetail extends EvalRunSummary {
  details: {
    idx: number;
    question: string;
    verdict: string;
    faithfulness: number;
    citation: number;
    should_refuse: boolean;
    refuse_ok: boolean | null;
  }[];
}

/** 监控大盘首屏指标（/admin/metrics/summary） */
export interface MetricsSummary {
  qps: number;
  total_requests: number;
  error_rate: number;
  p50_latency_ms: number;
  p95_latency_ms: number;
  p99_latency_ms: number;
  cache_hit_rate: number;
  llm_calls: number;
}

export interface TimeseriesPoint {
  ts: number;
  value: number;
}

export interface TopQuery {
  query: string;
  count: number;
  cache_hit_rate: number;
  avg_latency_ms: number;
  error_rate: number;
}

/** 运行时检索配置（/admin/retrieval-config） */
export interface RetrievalConfig {
  vector_enabled: boolean;
  bm25_enabled: boolean;
  vector_weight: number;
  bm25_weight: number;
  rerank_enabled: boolean;
  rerank_model: string;
  top_k: number;
  cache_ttl_s: number;
  ratelimit_per_min: number;
  ratelimit_capacity: number;
}

export interface RetrievalConfigView {
  active: RetrievalConfig;
  pending: RetrievalConfig | null;
  effective_at: number | null;
}

/** 知识库 KPI（/knowledge/stats） */
export interface KnowledgeStats {
  documents: number;
  indexed_documents: number;
  total_chunks: number;
  avg_retrieval_ms: number;
  last_indexed_at: string | null;
}

/** 单文档检索诊断命中（/knowledge/{doc_id}/probe） */
export interface ProbeHit {
  chunk_id: string;
  seq: number;
  score: number;
  source_route: string;
  source: string;
  content: string;
}

export interface ProbeResult {
  doc_id: string;
  query: string;
  reranked: boolean;
  stats: RetrievalStats;
  hits: ProbeHit[];
}

// ---------- 阶段 3：入库任务 ----------

/** 文件级 7 态 */
export type FileStage =
  | "pending" | "uploading" | "queued" | "parsing" | "chunking" | "indexing" | "done" | "failed";

export interface TaskFile {
  file_id: string;
  task_id: string;
  filename: string;
  size: number;
  status: FileStage;
  stage: string;
  stage_progress: number;
  stage_elapsed_ms: number;
  error_stage: string;
  error_reason: string;
  doc_id: string;
  stats: { chunks?: number; tables?: number; assets?: number; segments?: number; warnings?: string[] };
}

export interface IngestTask {
  task_id: string;
  status: "queued" | "running" | "done" | "partial_failed" | "failed" | "cancelled";
  parse_policy: Record<string, unknown>;
  chunk_policy: Record<string, unknown>;
  total_files: number;
  done_files: number;
  created_by: string;
  created_at: string;
  files: TaskFile[];
}

export interface TaskView {
  task: IngestTask;
  stats: { estimated_remaining_ms: number; done_files: number; failed_files: number; remaining_files: number };
}

/** 切块策略（向导第 3 步） */
export interface ChunkPolicy {
  strategy: "recursive" | "semantic" | "parent_child" | "raptor";
  params: Record<string, number>;
}

// ---------- 阶段 3：文档预览 ----------

export interface ChunkListItem {
  chunk_id: string;
  seq: number;
  preview: string;
  chunker: string;
  level: number;
  parent_id: string;
  modality: string;
  source: string;
  is_short: boolean;
}

export interface DocPreview {
  doc: KnowledgeFile;
  outline: { level: number; title: string }[];
  chunks: ChunkListItem[];
  parents: (ChunkListItem & { content: string })[];
  tables: { table_id: string; page: number; table_index: number; rows: number; cols: number; markdown: string }[];
  assets: { asset_id: string; uri: string; caption: string; kind: string }[];
  raptor_tree: { chunk_id: string; level: number; content: string; children: string[]; child_count: number }[];
  chunk_index: string[];
}

export interface ChunkDetail {
  chunk_id: string;
  content: string;
  position: string;
  chunker: string;
  level: number;
  modality: string;
  parent_id: string;
  parent_content: string | null;
  children: { chunk_id: string; preview: string }[];
  is_short: boolean;
}
