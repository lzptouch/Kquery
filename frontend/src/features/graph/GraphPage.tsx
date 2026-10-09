/** 图谱浏览器（阶段 7 前端规范 §二）。
 *
 * - 默认展示高连接度核心实体（不用空白画布面对用户）；
 * - 节点色板按实体五类；边带方向箭头 + 关系色板；
 * - 截断时黄色提示"仅展示部分关系"；
 * - 表格视图切换（键盘可达的可访问性兜底）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { DataState } from "../../components/DataState";
import { fetchEntityDetail, fetchGraphEntities, fetchSubgraph } from "../../lib/api-client";
import type { EntityDetail, GraphEntity, GraphLink, GraphNode, Subgraph } from "../../types";

const ENTITY_COLORS: Record<string, string> = {
  Person: "#3B82F6",
  Org: "#8B5CF6",
  Product: "#06B6D4",
  Concept: "#10B981",
  Event: "#F59E0B",
};
const RELATION_COLORS: Record<string, string> = {
  投资: "#DC2626",
  任职: "#3B82F6",
  收购: "#8B5CF6",
  关联: "#94A3B8",
  位于: "#06B6D4",
  影响: "#F59E0B",
};
const ENTITY_TYPES = ["Person", "Org", "Product", "Concept", "Event"];
const RELATION_TYPES = ["投资", "任职", "收购", "关联", "位于", "影响"];

interface PositionedNode extends GraphNode {
  x: number;
  y: number;
}

/** 轻量力导向布局：斥力 + 弹簧 + 向心，60 轮迭代收敛（<200 节点）。 */
function layout(nodes: GraphNode[], links: GraphLink[], w: number, h: number): PositionedNode[] {
  const pos: PositionedNode[] = nodes.map((n, i) => ({
    ...n,
    x: w / 2 + Math.cos((i / nodes.length) * Math.PI * 2) * Math.min(w, h) * 0.3,
    y: h / 2 + Math.sin((i / nodes.length) * Math.PI * 2) * Math.min(w, h) * 0.3,
  }));
  const index = new Map(pos.map((p, i) => [p.id, i]));
  for (let iter = 0; iter < 60; iter++) {
    const forces = pos.map(() => ({ fx: 0, fy: 0 }));
    // 斥力
    for (let i = 0; i < pos.length; i++) {
      for (let j = i + 1; j < pos.length; j++) {
        const dx = pos[i].x - pos[j].x;
        const dy = pos[i].y - pos[j].y;
        const dist2 = Math.max(dx * dx + dy * dy, 100);
        const f = 4000 / dist2;
        const dist = Math.sqrt(dist2);
        forces[i].fx += (dx / dist) * f;
        forces[i].fy += (dy / dist) * f;
        forces[j].fx -= (dx / dist) * f;
        forces[j].fy -= (dy / dist) * f;
      }
    }
    // 弹簧（边）
    for (const link of links) {
      const si = index.get(link.source);
      const ti = index.get(link.target);
      if (si == null || ti == null) continue;
      const dx = pos[ti].x - pos[si].x;
      const dy = pos[ti].y - pos[si].y;
      const dist = Math.max(Math.hypot(dx, dy), 1);
      const f = (dist - 120) * 0.02;
      forces[si].fx += (dx / dist) * f;
      forces[si].fy += (dy / dist) * f;
      forces[ti].fx -= (dx / dist) * f;
      forces[ti].fy -= (dy / dist) * f;
    }
    // 向心 + 应用
    for (let i = 0; i < pos.length; i++) {
      forces[i].fx += (w / 2 - pos[i].x) * 0.01;
      forces[i].fy += (h / 2 - pos[i].y) * 0.01;
      pos[i].x = Math.min(Math.max(pos[i].x + forces[i].fx, 40), w - 40);
      pos[i].y = Math.min(Math.max(pos[i].y + forces[i].fy, 30), h - 30);
    }
  }
  return pos;
}

export function GraphPage() {
  const [entities, setEntities] = useState<GraphEntity[]>([]);
  const [subgraph, setSubgraph] = useState<Subgraph | null>(null);
  const [selected, setSelected] = useState<EntityDetail | null>(null);
  const [search, setSearch] = useState("");
  const [typeFilter, setTypeFilter] = useState<Set<string>>(new Set(ENTITY_TYPES));
  const [relFilter, setRelFilter] = useState<Set<string>>(new Set(RELATION_TYPES));
  const [tableView, setTableView] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const svgRef = useRef<SVGSVGElement>(null);

  const load = useCallback(async (seed = "") => {
    setError(null);
    setLoading(true);
    try {
      const [ents, sub] = await Promise.all([fetchGraphEntities(), fetchSubgraph(seed, seed ? 2 : 1)]);
      if (ents.data) setEntities(ents.data);
      else if (!seed) setError(ents.error?.message ?? "图谱数据加载失败");
      if (sub.data) setSubgraph(sub.data);
    } catch {
      setError("图谱数据加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const handleSearch = useCallback(() => {
    if (search.trim()) void load(search.trim());
  }, [search, load]);

  const handleNodeClick = useCallback(async (name: string) => {
    const envelope = await fetchEntityDetail(name);
    if (envelope.data) setSelected(envelope.data);
  }, []);

  const filtered = useMemo(() => {
    if (!subgraph) return null;
    const nodes = subgraph.nodes.filter((n) => typeFilter.has(n.type));
    const nodeSet = new Set(nodes.map((n) => n.id));
    const links = subgraph.links.filter(
      (l) => nodeSet.has(l.source) && nodeSet.has(l.target) && relFilter.has(l.relation),
    );
    return { nodes, links };
  }, [subgraph, typeFilter, relFilter]);

  const positioned = useMemo(() => {
    if (!filtered || filtered.nodes.length === 0) return [];
    return layout(filtered.nodes, filtered.links, 640, 420);
  }, [filtered]);

  const posMap = useMemo(() => new Map(positioned.map((p) => [p.id, p])), [positioned]);

  const toggleSet = (set: Set<string>, value: string, setter: (s: Set<string>) => void) => {
    const next = new Set(set);
    if (next.has(value)) next.delete(value);
    else next.add(value);
    setter(next);
  };

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1200 }}>
        <div className="card" style={{ display: "flex", gap: 12, alignItems: "center" }}>
          <h3 style={{ margin: 0 }}>知识图谱</h3>
          <input
            className="probe-input"
            style={{ flex: 1, maxWidth: 320 }}
            placeholder="输入实体名搜索（回车聚焦）"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            aria-label="搜索实体"
          />
          <button type="button" className="btn" onClick={handleSearch}>定位</button>
          <button type="button" className="btn-text" onClick={() => { setSearch(""); void load(); }}>
            重置
          </button>
          <button type="button" className="btn-text" onClick={() => setTableView(!tableView)}>
            {tableView ? "画布视图" : "表格视图"}
          </button>
        </div>

        {error && <div className="error-inline" role="alert">⚠ {error}</div>}
        {subgraph?.truncated && (
          <div className="pending-note">⚠ 仅展示部分关系（前 100 条），可搜索实体缩小范围</div>
        )}

        <div className="graph-layout">
          {/* 左：实体列表 + 过滤 */}
          <div className="card graph-side">
            <div className="trace-id" style={{ marginBottom: 6 }}>类型过滤</div>
            {ENTITY_TYPES.map((t) => (
              <label key={t} className="graph-filter">
                <input
                  type="checkbox"
                  checked={typeFilter.has(t)}
                  onChange={() => toggleSet(typeFilter, t, setTypeFilter)}
                />
                <span className="graph-dot" style={{ background: ENTITY_COLORS[t] }} />
                {t}
              </label>
            ))}
            <div className="trace-id" style={{ margin: "10px 0 6px" }}>关系过滤</div>
            {RELATION_TYPES.map((r) => (
              <label key={r} className="graph-filter">
                <input
                  type="checkbox"
                  checked={relFilter.has(r)}
                  onChange={() => toggleSet(relFilter, r, setRelFilter)}
                />
                <span style={{ color: RELATION_COLORS[r] }}>—</span>
                {r}
              </label>
            ))}
            <div className="trace-id" style={{ margin: "10px 0 6px" }}>
              实体（{entities.length}）
            </div>
            <div className="graph-entity-list">
              {entities.slice(0, 50).map((e) => (
                <button
                  key={e.name}
                  type="button"
                  className="graph-entity-item"
                  onClick={() => void handleNodeClick(e.name)}
                >
                  <span className="graph-dot" style={{ background: ENTITY_COLORS[e.type] }} />
                  {e.name}
                  <span className="mono trace-id">{e.degree}</span>
                </button>
              ))}
            </div>
          </div>

          {/* 中：画布 / 表格 */}
          <div className="card graph-canvas-wrap">
            {tableView ? (
              <table className="files-table">
                <thead>
                  <tr><th>起点</th><th>关系</th><th>终点</th><th>强度</th></tr>
                </thead>
                <tbody>
                  {(filtered?.links ?? []).map((l, i) => (
                    <tr key={i}>
                      <td>{l.source}</td>
                      <td style={{ color: RELATION_COLORS[l.relation] }}>{l.relation} →</td>
                      <td>{l.target}</td>
                      <td className="mono">{l.weight}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <DataState
                loading={loading}
                error={error}
                empty={positioned.length === 0}
                emptyText="图谱为空"
                emptyHint="在知识库上传文档并等待入库完成，图谱会自动构建（也可手动调用「构建图谱」）。"
              >
              <div />
              <svg ref={svgRef} viewBox="0 0 640 420" className="graph-canvas" role="img" aria-label="知识图谱画布">
                <defs>
                  <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
                    <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--text-secondary)" />
                  </marker>
                </defs>
                {/* 不用 filtered! 非空断言：subgraph 未加载时 filtered 就是 null，
                    断言骗过了 TS，运行时直接抛 "Cannot read properties of null"，
                    整页白屏。这里显式兜底，加载/失败/空态由外层 DataState 负责。 */}
                {(filtered?.links ?? []).map((l, i) => {
                  const s = posMap.get(l.source);
                  const t = posMap.get(l.target);
                  if (!s || !t) return null;
                  const color = RELATION_COLORS[l.relation] ?? "#94A3B8";
                  const dashed = ["关联", "位于", "影响"].includes(l.relation);
                  const mx = (s.x + t.x) / 2;
                  const my = (s.y + t.y) / 2;
                  return (
                    <g key={i}>
                      <line
                        x1={s.x} y1={s.y} x2={t.x} y2={t.y}
                        stroke={color} strokeWidth={dashed ? 1 : 2}
                        strokeDasharray={dashed ? "4 3" : undefined}
                        markerEnd="url(#arrow)"
                        opacity={0.8}
                      />
                      <text x={mx} y={my - 4} fontSize="10" fill={color} textAnchor="middle">
                        {l.relation}
                      </text>
                    </g>
                  );
                })}
                {positioned.map((n) => (
                  <g
                    key={n.id}
                    transform={`translate(${n.x},${n.y})`}
                    onClick={() => void handleNodeClick(n.id)}
                    style={{ cursor: "pointer" }}
                    role="button"
                    aria-label={`实体 ${n.id}`}
                    tabIndex={0}
                    onKeyDown={(e) => e.key === "Enter" && void handleNodeClick(n.id)}
                  >
                    <circle
                      r={Math.min(10 + n.degree * 2, 22)}
                      fill={ENTITY_COLORS[n.type] ?? "#94A3B8"}
                      opacity={0.85}
                      stroke={selected?.name === n.id ? "#111" : "none"}
                      strokeWidth={2}
                    />
                    <text y={Math.min(10 + n.degree * 2, 22) + 12} fontSize="11" textAnchor="middle" fill="var(--text)">
                      {n.id.length > 8 ? `${n.id.slice(0, 8)}…` : n.id}
                    </text>
                  </g>
                ))}
              </svg>
              </DataState>
            )}
          </div>

          {/* 右：实体详情 */}
          <div className="card graph-detail">
            {selected ? (
              <dl className="entity-detail">
                <dt>实体</dt>
                <dd>
                  <span className="graph-dot" style={{ background: ENTITY_COLORS[selected.type] }} />
                  <strong>{selected.name}</strong>
                </dd>
                <dt>类型</dt><dd>{selected.type}</dd>
                {selected.aliases.length > 0 && (
                  <><dt>别名</dt><dd>{selected.aliases.join("、")}</dd></>
                )}
                {selected.description && (<><dt>描述</dt><dd>{selected.description}</dd></>)}
                {selected.community && (
                  <><dt>社区</dt><dd>{selected.community.community_id}（{selected.community.size} 成员）
                    {selected.community.summary && <div className="trace-id" style={{ marginTop: 4 }}>{selected.community.summary}</div>}
                  </dd></>
                )}
                <dt>出边关系</dt>
                <dd>
                  <ul role="list" className="edge-list">
                    {selected.out_edges.map((e, i) => (
                      <li key={i}>
                        <span style={{ color: RELATION_COLORS[e.relation] }}>{e.relation}</span>
                        {" → "}
                        <button type="button" className="btn-text" onClick={() => void handleNodeClick(e.to)}>
                          {e.to}
                        </button>
                      </li>
                    ))}
                    {selected.out_edges.length === 0 && <li className="trace-id">无</li>}
                  </ul>
                </dd>
                <dt>入边关系</dt>
                <dd>
                  <ul role="list" className="edge-list">
                    {selected.in_edges.map((e, i) => (
                      <li key={i}>
                        ← <span style={{ color: RELATION_COLORS[e.relation] }}>{e.relation}</span>
                        {" "}
                        <button type="button" className="btn-text" onClick={() => void handleNodeClick(e.from)}>
                          {e.from}
                        </button>
                      </li>
                    ))}
                    {selected.in_edges.length === 0 && <li className="trace-id">无</li>}
                  </ul>
                </dd>
              </dl>
            ) : (
              <div className="trace-id" style={{ padding: 12 }}>点击画布节点查看实体详情</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
