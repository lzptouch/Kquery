/** Query 分析历史（/knowledge/query-analysis，阶段 4 前端规范 §4）。
 *
 * 表格：时间 / 意图 / 改写数 / 子问题数 / 耗时；行点击展开详情（完整分析记录）。
 * 过滤：意图类型 / 是否检索。
 */
import { Fragment, useCallback, useEffect, useState } from "react";
import { IntentBadge, QueryAnalysisPanel } from "../chat/components/QueryAnalysisPanel";
import { request } from "../../lib/api-client";
import type { AnalysisRow, Envelope, QueryAnalysis } from "../../types";

const INTENT_OPTIONS = [
  { value: "", label: "全部意图" },
  { value: "fact_qa", label: "事实问答" },
  { value: "compare", label: "对比" },
  { value: "summarize", label: "总结" },
  { value: "calc", label: "计算" },
  { value: "chitchat", label: "闲聊" },
];

export function QueryAnalysisPage() {
  const [rows, setRows] = useState<AnalysisRow[] | null>(null);
  const [intent, setIntent] = useState("");
  const [needsRetrieval, setNeedsRetrieval] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [detail, setDetail] = useState<QueryAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const params = new URLSearchParams();
      if (intent) params.set("intent", intent);
      if (needsRetrieval) params.set("needs_retrieval", needsRetrieval);
      const envelope = await request<AnalysisRow[]>(`/knowledge/query-analysis?${params}`);
      if (envelope.data) setRows(envelope.data);
      else setError(envelope.error?.message ?? "加载失败");
    } catch {
      setError("网络异常");
    }
  }, [intent, needsRetrieval]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggleRow = async (analysisId: string) => {
    if (expanded === analysisId) {
      setExpanded(null);
      setDetail(null);
      return;
    }
    setExpanded(analysisId);
    const envelope: Envelope<QueryAnalysis> = await request(
      `/knowledge/query-analysis/${encodeURIComponent(analysisId)}`,
    );
    setDetail(envelope.data ?? null);
  };

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1080 }}>
        <div className="card" style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <h3 style={{ margin: 0, flex: 1 }}>Query 分析历史</h3>
          <select value={intent} onChange={(e) => setIntent(e.target.value)} aria-label="意图过滤">
            {INTENT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>{o.label}</option>
            ))}
          </select>
          <select value={needsRetrieval} onChange={(e) => setNeedsRetrieval(e.target.value)} aria-label="检索过滤">
            <option value="">全部</option>
            <option value="true">走检索</option>
            <option value="false">闲聊短路</option>
          </select>
          <button type="button" className="btn" onClick={() => void load()}>刷新</button>
        </div>

        {error && <div className="error-inline" role="alert">⚠ {error}</div>}

        <div className="card">
          {rows === null ? (
            <div className="skeleton" style={{ height: 200 }} />
          ) : rows.length === 0 ? (
            <div className="empty-state" style={{ padding: 24 }}>暂无分析记录，先到对话页提问。</div>
          ) : (
            <table className="files-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>问题</th>
                  <th>意图</th>
                  <th>改写数</th>
                  <th>子问题</th>
                  <th>耗时</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <Fragment key={r.analysis_id}>
                    <tr onClick={() => void toggleRow(r.analysis_id)} style={{ cursor: "pointer" }}>
                      <td className="trace-id">{r.created_at.slice(11, 19)}</td>
                      <td style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={r.raw_query}>
                        {r.raw_query}
                      </td>
                      <td><IntentBadge intent={r.intent} /></td>
                      <td className="mono">{r.rewrite_count}</td>
                      <td className="mono">{r.sub_question_count}</td>
                      <td className="mono">{r.elapsed_ms}ms</td>
                    </tr>
                    {expanded === r.analysis_id && detail && (
                      <tr>
                        <td colSpan={6}>
                          <QueryAnalysisPanel analysis={detail} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
