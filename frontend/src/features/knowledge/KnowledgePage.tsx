/** 知识库管理页（/knowledge）：上传 + 文件列表 + 状态标签 + 操作菜单。
 *
 * 四态：loading（骨架屏）/ empty（引导上传）/ error（inline + 重试）/ data（表格）。
 * "块数可见"是用户判断入库成功的直接证据，必须展示。
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { deleteFile, fetchKnowledgeBases, fetchKnowledgeStats, listFiles, offlineDocument, probeDocument, rebuildFile, reparseDocument, request } from "../../lib/api-client";
import { IntentBadge } from "../chat/components/QueryAnalysisPanel";
import { DocumentUploader } from "./components/DocumentUploader";
import { ProbeDialog } from "./components/ProbeDialog";
import type { KnowledgeFile, KnowledgeStats, QueryPattern } from "../../types";

const STATUS_LABEL: Record<string, string> = {
  uploaded: "已上传",
  parsing: "解析中",
  indexed: "已完成",
  failed: "失败",
  offline: "已下架",
};

function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-CN", { hour12: false });
}

function Copyable({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <span
      className="copyable mono"
      title={`点击复制：${text}`}
      onClick={() => {
        void navigator.clipboard.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
    >
      {copied ? "已复制" : text}
    </span>
  );
}

export function KnowledgePage() {
  const navigate = useNavigate();
  const [files, setFiles] = useState<KnowledgeFile[] | null>(null);
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [patterns, setPatterns] = useState<QueryPattern[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const [busyDoc, setBusyDoc] = useState<string | null>(null);
  const [probeDoc, setProbeDoc] = useState<KnowledgeFile | null>(null);
  // kb_id → 展示名映射：文档只带 knowledge_base_id（机器 slug），
  // 直接展示会让用户看到「ui测试库97616」这类标识而不是人读名称。
  const [kbNames, setKbNames] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setError(null);
    try {
      const envelope = await listFiles();
      if (envelope.status === "error" || !envelope.data) {
        setError(envelope.error?.message ?? "加载失败");
        setFiles([]);
        return;
      }
      setFiles(envelope.data);
      // KB 名称映射：文档只带 knowledge_base_id，展示名要从 KB 列表反查
      const kbEnvelope = await fetchKnowledgeBases();
      if (kbEnvelope.data) {
        setKbNames(Object.fromEntries(kbEnvelope.data.map((k) => [k.kb_id, k.name])));
      }
      const statsEnvelope = await fetchKnowledgeStats();
      if (statsEnvelope.data) setStats(statsEnvelope.data);
      const patternsEnvelope = await request<QueryPattern[]>("/knowledge/query-patterns");
      if (patternsEnvelope.data) setPatterns(patternsEnvelope.data);
    } catch {
      setError("网络异常，请检查后端服务是否启动");
      setFiles([]);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleRebuild = async (docId: string) => {
    setOpenMenu(null);
    setBusyDoc(docId);
    await rebuildFile(docId).catch(() => null);
    await load();
    setBusyDoc(null);
  };

  const handleDelete = async (file: KnowledgeFile) => {
    setOpenMenu(null);
    const ok = window.confirm(
      `确认删除「${file.filename}」？\n\n影响范围：该文档及其 ${file.chunks} 个切块将从知识库中移除，相关引用将不可追溯。\n此操作不可恢复。`,
    );
    if (!ok) return;
    setBusyDoc(file.doc_id);
    await deleteFile(file.doc_id).catch(() => null);
    await load();
    setBusyDoc(null);
  };

  const handleOffline = async (file: KnowledgeFile) => {
    setOpenMenu(null);
    const ok = window.confirm(
      `确认删除索引（临时下架）「${file.filename}」？\n\n影响范围：文档从检索中消失，但源文件与记录保留，可随时重建恢复。\n与"删除文档"不同，此操作可逆。`,
    );
    if (!ok) return;
    setBusyDoc(file.doc_id);
    await offlineDocument(file.doc_id).catch(() => null);
    await load();
    setBusyDoc(null);
  };

  const handleReparse = async (file: KnowledgeFile) => {
    setOpenMenu(null);
    const strategy = window.prompt(
      "选择切块策略重新解析（生成新版本块集合）：\nrecursive / semantic / parent_child / raptor",
      "semantic",
    );
    if (!strategy) return;
    setBusyDoc(file.doc_id);
    await reparseDocument(file.doc_id, { strategy, params: {} }).catch(() => null);
    await load();
    setBusyDoc(null);
  };

  return (
    <div className="page">
      <div className="page-inner">
        {/* 检索 KPI 看板 */}
        {stats && (
          <div className="kpi-row">
            <span className="kpi-item">
              索引文档数 <strong className="mono">{stats.indexed_documents}</strong>
            </span>
            <span className="kpi-item">
              总 Chunk 数 <strong className="mono">{stats.total_chunks}</strong>
            </span>
            <span className="kpi-item">
              平均检索耗时 <strong className="mono">{stats.avg_retrieval_ms}ms</strong>
            </span>
            <span className="kpi-item">
              最近索引 <strong>{stats.last_indexed_at ? formatTime(stats.last_indexed_at) : "-"}</strong>
            </span>
          </div>
        )}

        <div className="card">
          <div style={{ display: "flex", alignItems: "center" }}>
            <h3 style={{ marginTop: 0, flex: 1 }}>上传文档</h3>
            <Link to="/knowledge/upload-advanced" className="btn" style={{ textDecoration: "none" }}>
              高级上传（多模态 / 切块策略）→
            </Link>
          </div>
          <DocumentUploader onUploaded={load} />
        </div>

        {/* Query 模式分析 */}
        {patterns.length > 0 && (
          <div className="card">
            <div style={{ display: "flex", alignItems: "center" }}>
              <h3 style={{ marginTop: 0, flex: 1 }}>Query 模式分析</h3>
              <Link to="/knowledge/query-analysis" className="btn-text" style={{ textDecoration: "none" }}>
                查看分析历史 →
              </Link>
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {patterns.slice(0, 6).map((p) => (
                <div key={p.intent} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <span className="mono" style={{ width: 48, textAlign: "right" }}>
                    {Math.round(p.ratio * 100)}%
                  </span>
                  <IntentBadge intent={p.intent} />
                  <span className="trace-id">{p.count} 次 · 平均预处理 {p.avg_elapsed_ms}ms</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="card">
          <h3 style={{ marginTop: 0 }}>知识库文件</h3>

          {files === null ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              {[0, 1, 2].map((i) => (
                <div key={i} className="skeleton" style={{ height: 40 }} />
              ))}
            </div>
          ) : error ? (
            <div className="error-inline" role="alert">
              <span aria-hidden>⚠</span>
              <span>{error}</span>
              <button type="button" className="btn-text" onClick={() => void load()}>
                重试
              </button>
            </div>
          ) : files.length === 0 ? (
            <div className="empty-state">
              <p>暂无文档，先在上方上传 PDF 或 Markdown 文件。</p>
            </div>
          ) : (
            <table className="files-table">
              <thead>
                <tr>
                  <th>文件名</th>
                  <th>所属知识库</th>
                  <th>状态</th>
                  <th>切块数</th>
                  <th>上传时间</th>
                  <th>标识</th>
                  <th style={{ width: 60 }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {files.map((f) => (
                  <tr key={f.doc_id}>
                    <td>{f.filename}</td>
                    <td className="mono">
                      {kbNames[f.knowledge_base_id || "default"] ?? (f.knowledge_base_id || "default")}
                    </td>
                    <td>
                      <span
                        className={`status-tag ${f.status}`}
                        title={f.status === "failed" ? f.error ?? "未知原因" : undefined}
                      >
                        {busyDoc === f.doc_id ? "处理中…" : STATUS_LABEL[f.status]}
                      </span>
                    </td>
                    <td className="mono">{f.status === "indexed" ? f.chunks : "-"}</td>
                    <td className="trace-id">{formatTime(f.uploaded_at)}</td>
                    <td className="trace-id">
                      <Copyable text={f.doc_id} />
                    </td>
                    <td>
                      <div className="menu-wrap">
                        <button
                          type="button"
                          className="btn-text"
                          aria-label="更多操作"
                          onClick={() => setOpenMenu(openMenu === f.doc_id ? null : f.doc_id)}
                        >
                          ⋮
                        </button>
                        {openMenu === f.doc_id && (
                          <div className="menu-pop" onMouseLeave={() => setOpenMenu(null)}>
                            <button
                              type="button"
                              onClick={() => {
                                setOpenMenu(null);
                                setProbeDoc(f);
                              }}
                            >
                              测试检索
                            </button>
                            <button
                              type="button"
                              onClick={() => navigate(`/knowledge/preview/${encodeURIComponent(f.doc_id)}`)}
                            >
                              查看解析详情
                            </button>
                            <button type="button" onClick={() => void handleRebuild(f.doc_id)}>
                              重新构建索引
                            </button>
                            <button type="button" onClick={() => void handleReparse(f)}>
                              重新解析（新策略）
                            </button>
                            <button type="button" onClick={() => void handleOffline(f)}>
                              删除索引（下架）
                            </button>
                            <button type="button" className="danger" onClick={() => void handleDelete(f)}>
                              删除文档
                            </button>
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {probeDoc && (
        <ProbeDialog doc={probeDoc} onProbe={probeDocument} onClose={() => setProbeDoc(null)} />
      )}
    </div>
  );
}
