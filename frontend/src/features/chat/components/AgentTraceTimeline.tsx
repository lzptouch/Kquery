/** Agent 执行轨迹时间轴。
 *
 * 节点色板：plan 紫 / tool 蓝 / reflect 琥珀 / answer 绿 / replan 红。
 * 执行中的节点带渐变流动动画；失败节点红边框；熔断节点标黄。
 * 点击 tool 节点展开 ToolCallCard（输入/输出/耗时/命中数）。
 */
import { useState } from "react";
import type { TraceNode } from "../../../types";

const KIND_META: Record<TraceNode["kind"], { color: string; icon: string; label: string }> = {
  plan: { color: "#8b5cf6", icon: "🧭", label: "规划" },
  tool: { color: "#3b82f6", icon: "🔧", label: "工具" },
  reflect: { color: "#f59e0b", icon: "🔍", label: "反思" },
  answer: { color: "#16a34a", icon: "✅", label: "作答" },
  replan: { color: "#dc2626", icon: "🔄", label: "重规划" },
};

function ToolCallCard({ node }: { node: TraceNode }) {
  return (
    <div className="tool-call-card">
      <div className="tool-call-head mono">
        🔧 {node.tool}
        {node.elapsedMs != null && <span>耗时 {node.elapsedMs}ms</span>}
        {node.hitCount != null && <span>命中 {node.hitCount} 条</span>}
      </div>
      {node.input && (
        <pre className="tool-call-io">输入: {node.input}</pre>
      )}
      {node.error ? (
        <pre className="tool-call-io tool-call-error">错误: {node.error}</pre>
      ) : (
        node.hitCount === 0 && <pre className="tool-call-io">输出: 无结果（0 条）</pre>
      )}
    </div>
  );
}

export function AgentTraceTimeline({
  nodes,
  totalMs,
  fused,
}: {
  nodes: TraceNode[];
  totalMs?: number;
  fused?: boolean;
}) {
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  if (nodes.length === 0) return null;

  return (
    <div className="trace-timeline" role="list" aria-label="Agent 执行轨迹">
      <div className="trace-timeline-title">🤖 Agent 执行轨迹</div>
      {nodes.map((node, i) => {
        const meta = KIND_META[node.kind];
        const clickable = node.kind === "tool";
        return (
          <div key={i} role="listitem">
            <button
              type="button"
              className={`trace-node${node.running ? " running" : ""}${node.error ? " failed" : ""}`}
              style={{ borderLeftColor: meta.color }}
              onClick={() => clickable && setExpandedIdx(expandedIdx === i ? null : i)}
              aria-expanded={clickable ? expandedIdx === i : undefined}
              tabIndex={0}
            >
              <span className="trace-node-icon">{meta.icon}</span>
              <span className="trace-node-body">
                <span className="trace-node-title">
                  {node.title}
                  {node.running && <span className="trace-running-dot"> ●</span>}
                </span>
                {node.detail && <span className="trace-node-detail">{node.detail}</span>}
              </span>
              {node.elapsedMs != null && (
                <span className="mono trace-node-ms">{node.elapsedMs}ms</span>
              )}
            </button>
            {expandedIdx === i && node.kind === "tool" && <ToolCallCard node={node} />}
          </div>
        );
      })}
      {fused && (
        <div className="trace-fuse">⚠ 已达执行上限，结果基于现有证据生成，可能不完整</div>
      )}
      {totalMs != null && (
        <div className="trace-timeline-foot mono">
          总步骤: {nodes.length}　总耗时: {(totalMs / 1000).toFixed(1)}s
        </div>
      )}
    </div>
  );
}
