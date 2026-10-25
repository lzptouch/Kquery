/** 监控大盘（/admin/monitor， §3）。
 *
 * 5 个 MetricCard（QPS / 错误率 / P95 延迟 / 缓存命中率 / LLM 调用）+
 * 趋势图（SVG 迷你折线）+ Top Queries 表。数值必配阈值变色与文字趋势。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchAgentMetrics, fetchGraphStats, fetchMetricsSummary, fetchQualityMetrics, fetchTimeseries, fetchTopQueries } from "../../lib/api-client";
import type { AgentMetrics, GraphStats, MetricsSummary, QualityMetrics, TimeseriesPoint, TopQuery } from "../../types";

const RANGES = [
  { label: "近 15m", value: 900 },
  { label: "近 1h", value: 3600 },
  { label: "近 6h", value: 21600 },
  { label: "近 24h", value: 86400 },
];

function Sparkline({ points, color = "var(--primary)" }: { points: TimeseriesPoint[]; color?: string }) {
  if (points.length < 2) return <div className="sparkline-empty">数据积累中…</div>;
  const w = 220;
  const h = 40;
  const max = Math.max(...points.map((p) => p.value), 0.001);
  const path = points
    .map((p, i) => `${(i / (points.length - 1)) * w},${h - (p.value / max) * (h - 4)}`)
    .join(" ");
  return (
    <svg width={w} height={h} className="sparkline" aria-hidden>
      <polyline points={path} fill="none" stroke={color} strokeWidth="2" />
    </svg>
  );
}

function MetricCard({
  title,
  value,
  unit,
  alert,
  children,
  ariaLabel,
}: {
  title: string;
  value: string;
  unit?: string;
  alert?: boolean;
  children?: React.ReactNode;
  ariaLabel: string;
}) {
  return (
    <div className={`metric-card${alert ? " alert" : ""}`} aria-label={ariaLabel}>
      <div className="metric-title">{title}</div>
      <div className="metric-value">
        {value}
        {unit && <span className="metric-unit">{unit}</span>}
      </div>
      {children}
    </div>
  );
}

export function MonitorPage() {
  const [rangeS, setRangeS] = useState(3600);
  const [summary, setSummary] = useState<MetricsSummary | null>(null);
  const [quality, setQuality] = useState<QualityMetrics | null>(null);
  const [requestsTs, setRequestsTs] = useState<TimeseriesPoint[]>([]);
  const [latencyTs, setLatencyTs] = useState<TimeseriesPoint[]>([]);
  const [cacheTs, setCacheTs] = useState<TimeseriesPoint[]>([]);
  const [topQueries, setTopQueries] = useState<TopQuery[]>([]);
  const [agent, setAgent] = useState<AgentMetrics | null>(null);
  const [graphStats, setGraphStats] = useState<GraphStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      const [s, req, lat, cache, top, q, ag, gs] = await Promise.all([
        fetchMetricsSummary(rangeS),
        fetchTimeseries("requests", rangeS),
        fetchTimeseries("p95_latency_ms", rangeS),
        fetchTimeseries("cache_hit_rate", rangeS),
        fetchTopQueries(10),
        fetchQualityMetrics(),
        fetchAgentMetrics(),
        fetchGraphStats(),
      ]);
      if (s.data) setSummary(s.data);
      if (q.data) setQuality(q.data);
      if (req.data) setRequestsTs(req.data.points);
      if (lat.data) setLatencyTs(lat.data.points);
      if (cache.data) setCacheTs(cache.data.points);
      if (top.data) setTopQueries(top.data);
      if (ag.data) setAgent(ag.data);
      if (gs.data) setGraphStats(gs.data);
    } catch {
      setError("监控数据加载失败，请检查后端服务");
    }
  }, [rangeS]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30000); // 非实时面板 30s 轮询
    return () => clearInterval(timer);
  }, [load]);

  const pct = (v: number) => `${(v * 100).toFixed(1)}%`;

  return (
    <div className="page">
      <div className="page-inner">
        <div className="card" style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <h3 style={{ margin: 0, flex: 1 }}>监控大盘</h3>
          <select value={rangeS} onChange={(e) => setRangeS(Number(e.target.value))} aria-label="时间范围">
            {RANGES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
          <button type="button" className="btn" onClick={() => void load()}>
            刷新
          </button>
        </div>

        {error && (
          <div className="error-inline" role="alert">
            <span aria-hidden>⚠</span>
            {error}
          </div>
        )}

        <div className="metric-grid">
          <MetricCard title="QPS" value={summary ? String(summary.qps) : "-"} ariaLabel={`QPS ${summary?.qps ?? "未知"}`}>
            <Sparkline points={requestsTs} />
          </MetricCard>
          <MetricCard
            title="错误率"
            value={summary ? pct(summary.error_rate) : "-"}
            alert={(summary?.error_rate ?? 0) > 0.05}
            ariaLabel={`错误率 ${summary ? pct(summary.error_rate) : "未知"}`}
          >
            <Sparkline points={requestsTs} color="var(--error)" />
          </MetricCard>
          <MetricCard
            title="P95 延迟"
            value={summary ? String(summary.p95_latency_ms) : "-"}
            unit="ms"
            alert={(summary?.p95_latency_ms ?? 0) > 2000}
            ariaLabel={`P95 延迟 ${summary?.p95_latency_ms ?? "未知"} 毫秒`}
          >
            <Sparkline points={latencyTs} color="var(--warning)" />
          </MetricCard>
          <MetricCard title="缓存命中率" value={summary ? pct(summary.cache_hit_rate) : "-"} ariaLabel={`缓存命中率 ${summary ? pct(summary.cache_hit_rate) : "未知"}`}>
            <Sparkline points={cacheTs} color="var(--success)" />
          </MetricCard>
          <MetricCard title="LLM 调用" value={summary ? String(summary.llm_calls) : "-"} ariaLabel={`LLM 调用 ${summary?.llm_calls ?? "未知"} 次`}>
            <span className="trace-id">时间范围内累计</span>
          </MetricCard>
        </div>

        {/* 质量指标区 */}
        {quality && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>答案质量</h3>
            <div className="metric-grid">
              <MetricCard
                title="Faithfulness"
                value={quality.faithfulness != null ? quality.faithfulness.toFixed(2) : "未知"}
                alert={quality.faithfulness != null && quality.faithfulness < 0.85}
                ariaLabel={`Faithfulness ${quality.faithfulness ?? "未知"}`}
              >
                <span className="trace-id">答案-证据一致性均值（目标 ≥ 0.85）</span>
              </MetricCard>
              <MetricCard
                title="拒答率"
                value={pct(quality.refusal_rate)}
                alert={quality.refusal_rate > 0.3}
                ariaLabel={`拒答率 ${pct(quality.refusal_rate)}`}
              >
                <span className="trace-id">{quality.refused} 拒答 / {quality.answered + quality.refused} 总请求</span>
              </MetricCard>
              <MetricCard
                title="平均重生成"
                value={quality.avg_regeneration.toFixed(2)}
                ariaLabel={`平均重生成 ${quality.avg_regeneration}`}
              >
                <span className="trace-id">重生成率上升意味着证据质量下降</span>
              </MetricCard>
            </div>
            <div style={{ marginTop: 12 }}>
              <div className="trace-id" style={{ marginBottom: 6 }}>CRAG 分支占比</div>
              <div className="crag-ratio">
                {(["correct", "ambiguous", "incorrect"] as const).map((branch) => {
                  const ratio = quality.crag_branch_ratio[branch] ?? 0;
                  const color = branch === "correct" ? "var(--success)" : branch === "ambiguous" ? "var(--info)" : "var(--warning)";
                  return (
                    <div key={branch} className="crag-ratio-row">
                      <span className="trace-id" style={{ width: 80 }}>{branch}</span>
                      <div className="confidence-track" style={{ flex: 1 }}>
                        <div className="confidence-fill" style={{ width: `${ratio * 100}%`, background: color }} />
                      </div>
                      <span className="mono" style={{ width: 48, textAlign: "right" }}>{pct(ratio)}</span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        )}

        {/* 图谱与多模态指标区 */}
        {graphStats && (graphStats.entities > 0 || graphStats.images > 0) && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>图谱与多模态</h3>
            <div className="metric-grid">
              <MetricCard title="实体总数" value={String(graphStats.entities)} ariaLabel={`实体 ${graphStats.entities} 个`}>
                <span className="trace-id">知识图谱节点数</span>
              </MetricCard>
              <MetricCard title="关系总数" value={String(graphStats.relations)} ariaLabel={`关系 ${graphStats.relations} 条`}>
                <span className="trace-id">有向边</span>
              </MetricCard>
              <MetricCard title="社区数" value={String(graphStats.communities)} ariaLabel={`社区 ${graphStats.communities} 个`}>
                <span className="trace-id">连通分量检测</span>
              </MetricCard>
              <MetricCard
                title="孤立实体占比"
                value={pct(graphStats.orphan_ratio)}
                alert={graphStats.orphan_ratio > 0.2}
                ariaLabel={`孤立实体占比 ${pct(graphStats.orphan_ratio)}`}
              >
                <span className="trace-id">&gt;20% 说明关系抽取质量下降</span>
              </MetricCard>
              <MetricCard title="图片库规模" value={String(graphStats.images)} ariaLabel={`图像 ${graphStats.images} 张`}>
                <span className="trace-id mono">{graphStats.caption_model}</span>
              </MetricCard>
            </div>
          </div>
        )}

        {/* Agent 执行指标区 */}
        {agent && agent.runs > 0 && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Agent 执行</h3>
            <div className="metric-grid">
              <MetricCard title="平均迭代次数" value={String(agent.avg_iterations)} ariaLabel={`平均迭代 ${agent.avg_iterations} 次`}>
                <span className="trace-id">反映任务复杂度</span>
              </MetricCard>
              <MetricCard title="熔断触发率" value={pct(agent.fuse_rate)} alert={agent.fuse_rate > 0.05} ariaLabel={`熔断率 ${pct(agent.fuse_rate)}`}>
                <span className="trace-id">先于答案质量下降的预警信号</span>
              </MetricCard>
              <MetricCard title="Agent 执行次数" value={String(agent.runs)} ariaLabel={`执行 ${agent.runs} 次`}>
                <span className="trace-id">时间范围内累计</span>
              </MetricCard>
            </div>
            <div style={{ marginTop: 12 }}>
              <div className="trace-id" style={{ marginBottom: 6 }}>工具调用分布</div>
              {Object.entries(agent.tool_distribution).map(([name, count]) => {
                const total = Object.values(agent.tool_distribution).reduce((a, b) => a + b, 0);
                return (
                  <div key={name} className="crag-ratio-row">
                    <span className="trace-id" style={{ width: 120 }}>{name}</span>
                    <div className="confidence-track" style={{ flex: 1 }}>
                      <div className="confidence-fill" style={{ width: `${(count / Math.max(total, 1)) * 100}%`, background: "#3b82f6" }} />
                    </div>
                    <span className="mono" style={{ width: 48, textAlign: "right" }}>{count}</span>
                  </div>
                );
              })}
            </div>
            {agent.abnormal.length > 0 && (
              <div style={{ marginTop: 12 }}>
                <div className="trace-id" style={{ marginBottom: 6 }}>最近异常执行</div>
                {agent.abnormal.slice(0, 5).map((a, i) => (
                  <div key={i} className="trace-id" style={{ padding: "2px 0" }}>
                    {new Date(a.ts * 1000).toLocaleTimeString("zh-CN", { hour12: false })}　…{a.session_id}　{a.kind === "circuit_breaker" ? "熔断" : "工具失败"}（{a.detail}）
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="card">
          <h3 style={{ marginTop: 0 }}>Top Queries</h3>
          {topQueries.length === 0 ? (
            <div className="empty-state" style={{ padding: 24 }}>
              暂无问答数据
            </div>
          ) : (
            <table className="files-table">
              <thead>
                <tr>
                  <th>Query</th>
                  <th>调用数</th>
                  <th>缓存命中率</th>
                  <th>平均延迟</th>
                  <th>错误率</th>
                </tr>
              </thead>
              <tbody>
                {topQueries.map((q) => (
                  <tr key={q.query}>
                    <td style={{ maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={q.query}>
                      {q.query}
                    </td>
                    <td className="mono">{q.count}</td>
                    <td className="mono">{pct(q.cache_hit_rate)}</td>
                    <td className="mono">{q.avg_latency_ms}ms</td>
                    <td className="mono" style={{ color: q.error_rate > 0.05 ? "var(--error)" : undefined }}>
                      {pct(q.error_rate)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
