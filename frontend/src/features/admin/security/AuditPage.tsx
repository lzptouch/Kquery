/** 审计日志：过滤 + 列表 + 详情浮层 + CSV 导出。
 *
 * 审计不存原文：问题/答案只展示 hash；敏感命中只展示类别数。
 * 按 trace_id 聚合同一请求的全部事件按时序展示。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchAudit } from "../../../lib/api-client";
import type { AuditEvent } from "../../../types";

const EVENT_LABEL: Record<string, string> = {
  "rag.request": "请求",
  "rag.retrieve": "检索",
  "rag.answer": "回答",
  "admin.action": "管理操作",
};

function summarize(e: AuditEvent): string {
  const p = e.payload as Record<string, unknown>;
  switch (e.event) {
    case "rag.request":
      return `提问 hash: ${p.question_hash}（${p.question_len} 字）`;
    case "rag.retrieve":
      return `召回 ${p.retrieved} 条 / 可见 ${p.visible} 条`;
    case "rag.answer":
      return `答案 hash: ${p.answer_hash} · 敏感命中 ${p.sensitive_hits} 类`;
    case "admin.action":
      return `${p.action}`;
    default:
      return JSON.stringify(p).slice(0, 60);
  }
}

export function AuditPage() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [rangeS, setRangeS] = useState(3600);
  const [eventType, setEventType] = useState("");
  const [traceId, setTraceId] = useState("");
  const [detail, setDetail] = useState<AuditEvent | null>(null);
  const [related, setRelated] = useState<AuditEvent[]>([]);

  const load = useCallback(async () => {
    const envelope = await fetchAudit({
      range_s: rangeS,
      event: eventType || undefined,
      trace_id: traceId || undefined,
    });
    if (envelope.data) setEvents(envelope.data);
  }, [rangeS, eventType, traceId]);

  useEffect(() => {
    void load();
  }, [load]);

  const openDetail = async (e: AuditEvent) => {
    setDetail(e);
    const envelope = await fetchAudit({ range_s: 86400 * 30, trace_id: e.trace_id });
    if (envelope.data) {
      setRelated([...envelope.data].sort((a, b) => a.created_at - b.created_at));
    }
  };

  return (
    <div className="page">
      <div className="page-inner">
        <div className="card" style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
          <h3 style={{ margin: 0 }}>审计日志</h3>
          <select className="probe-input" style={{ width: 110 }} value={rangeS} onChange={(e) => setRangeS(Number(e.target.value))} aria-label="时间范围">
            <option value={3600}>近 1h</option>
            <option value={86400}>近 24h</option>
            <option value={604800}>近 7 天</option>
          </select>
          <select className="probe-input" style={{ width: 120 }} value={eventType} onChange={(e) => setEventType(e.target.value)} aria-label="事件类型">
            <option value="">全部事件</option>
            <option value="rag.request">请求</option>
            <option value="rag.retrieve">检索</option>
            <option value="rag.answer">回答</option>
            <option value="admin.action">管理操作</option>
          </select>
          <input
            className="probe-input" style={{ width: 180 }} placeholder="trace_id 精确搜索"
            value={traceId} onChange={(e) => setTraceId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void load()}
            aria-label="追踪号搜索"
          />
          <button type="button" className="btn" onClick={() => void load()}>查询</button>
          <a
            className="btn-text"
            href={`/admin/audit/export?range_s=${rangeS}`}
            onClick={(e) => {
              if (!window.confirm("导出审计日志（不含原文，仅哈希与元数据）？")) e.preventDefault();
            }}
          >
            导出 CSV
          </a>
        </div>

        <div className="card">
          {events.length === 0 ? (
            <div className="empty-state" style={{ padding: 24 }}>该范围内暂无审计事件</div>
          ) : (
            <table className="files-table">
              <thead>
                <tr><th>时间</th><th>trace_id</th><th>主体</th><th>事件</th><th>摘要</th></tr>
              </thead>
              <tbody>
                {events.map((e) => (
                  <tr key={e.event_id} style={{ cursor: "pointer" }} onClick={() => void openDetail(e)}>
                    <td className="mono">{new Date(e.created_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })}</td>
                    <td className="mono trace-id">{e.trace_id}</td>
                    <td className="mono">{e.tenant_id}:{e.user_id}</td>
                    <td><span className="issue-tag resolved">{EVENT_LABEL[e.event] ?? e.event}</span></td>
                    <td className="trace-id">{summarize(e)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {detail && (
          <div className="modal-mask" onClick={() => setDetail(null)}>
            <div className="modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-label="事件详情">
              <h3>事件详情</h3>
              <dl className="entity-detail">
                <dt>trace_id</dt><dd className="mono">{detail.trace_id}</dd>
                <dt>主体</dt><dd className="mono">{detail.tenant_id}:{detail.user_id}</dd>
                <dt>事件</dt><dd>{EVENT_LABEL[detail.event] ?? detail.event}</dd>
                <dt>时间</dt><dd>{new Date(detail.created_at * 1000).toLocaleString("zh-CN", { hour12: false })}</dd>
                <dt>payload</dt>
                <dd><pre className="tool-call-io">{JSON.stringify(detail.payload, null, 2)}</pre></dd>
              </dl>
              {related.length > 1 && (
                <>
                  <div className="trace-id" style={{ margin: "8px 0 4px" }}>同一请求的关联事件（按时序）</div>
                  {related.map((r) => (
                    <div key={r.event_id} className="trace-id" style={{ padding: "2px 0" }}>
                      {new Date(r.created_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })}　{EVENT_LABEL[r.event] ?? r.event}　{summarize(r)}
                    </div>
                  ))}
                </>
              )}
              <div style={{ marginTop: 12, textAlign: "right" }}>
                <button type="button" className="btn" onClick={() => setDetail(null)}>关闭</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
