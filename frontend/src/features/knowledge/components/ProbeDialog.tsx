/** 单文档检索诊断弹窗。
 *
 * 输入测试 query → 返回该文档被命中的 chunk 列表（按分数降序），
 * 可调 top_k 实时看命中变化——"入库成功不等于能被检索到"的验证工具。
 */
import { useState } from "react";
import type { Envelope, KnowledgeFile, ProbeResult } from "../../../types";

interface Props {
  doc: KnowledgeFile;
  onProbe: (docId: string, query: string, topK: number) => Promise<Envelope<ProbeResult>>;
  onClose: () => void;
}

const ROUTE_LABEL: Record<string, string> = {
  keyword: "关键词",
  vector: "语义",
  both: "融合",
};

export function ProbeDialog({ doc, onProbe, onClose }: Props) {
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(4);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ProbeResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const envelope = await onProbe(doc.doc_id, query.trim(), topK);
      if (envelope.data) setResult(envelope.data);
      else setError(envelope.error?.message ?? "诊断失败");
    } catch {
      setError("网络异常");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="modal-mask" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="检索诊断">
        <h3>测试检索：{doc.filename}</h3>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            type="text"
            style={{ flex: 1, padding: "8px 12px", border: "1px solid var(--border)", borderRadius: 6, font: "inherit" }}
            placeholder="输入测试 query…"
            value={query}
            aria-label="测试 query"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void run()}
          />
          <label className="trace-id">
            top_k
            <input
              type="number"
              min={1}
              max={10}
              style={{ width: 56, marginLeft: 4, padding: "6px", border: "1px solid var(--border)", borderRadius: 6 }}
              value={topK}
              onChange={(e) => setTopK(Math.max(1, Math.min(10, Number(e.target.value))))}
            />
          </label>
          <button type="button" className="btn btn-primary" disabled={loading || !query.trim()} onClick={() => void run()}>
            {loading ? "检索中…" : "检索"}
          </button>
        </div>

        {error && (
          <div className="error-inline" style={{ marginTop: 12 }} role="alert">
            <span aria-hidden>⚠</span>
            {error}
          </div>
        )}

        {result && (
          <div style={{ marginTop: 12 }}>
            <div className="trace-id" style={{ marginBottom: 8 }}>
              关键词候选 {result.stats.keyword_candidates} · 语义候选 {result.stats.vector_candidates} ·
              融合 {result.stats.fused_count} · {result.reranked ? "已精排" : "未精排"}
            </div>
            {result.hits.length === 0 ? (
              <div className="empty-state" style={{ padding: 16 }}>
                该文档没有命中块。可换用文档中的原文术语再试。
              </div>
            ) : (
              result.hits.map((h) => (
                <div key={h.chunk_id} className="probe-hit">
                  <div className="trace-id" style={{ display: "flex", gap: 8 }}>
                    <span className="mono">{h.chunk_id}</span>
                    <span className={`route-tag route-${h.source_route}`}>{ROUTE_LABEL[h.source_route] ?? h.source_route}</span>
                    <span className="mono">相关度 {h.score.toFixed(4)}</span>
                  </div>
                  <div style={{ marginTop: 4 }}>{h.content}</div>
                </div>
              ))
            )}
          </div>
        )}

        <div style={{ marginTop: 16, textAlign: "right" }}>
          <button type="button" className="btn" onClick={onClose}>
            关闭
          </button>
        </div>
      </div>
    </div>
  );
}
