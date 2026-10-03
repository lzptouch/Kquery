/** Query 分析详情面板（阶段 4 前端规范 §2.2）。
 *
 * 四段展示：意图（IntentBadge）/ 改写（QueryRewriteDiff）/ 子问题（SubQuestionList）/ 数据源路由。
 * 降级如实标注（哪一步未生效），提示强度克制（面板内标注，不弹窗）。
 */
import type { QueryAnalysis } from "../../../types";

const INTENT_STYLE: Record<string, { label: string; color: string }> = {
  chitchat: { label: "闲聊", color: "#64748b" },
  fact_qa: { label: "事实问答", color: "#3b82f6" },
  compare: { label: "对比", color: "#8b5cf6" },
  summarize: { label: "总结", color: "#0891b2" },
  calc: { label: "计算", color: "#16a34a" },
  code: { label: "代码", color: "#d97706" },
  routing: { label: "操作", color: "#ec4899" },
};

const REWRITE_STYLE: Record<string, { label: string; color: string; icon: string }> = {
  original: { label: "原始", color: "#64748b", icon: "💬" },
  rewrite: { label: "改写", color: "#3b82f6", icon: "✏️" },
  hyde: { label: "HyDE", color: "#8b5cf6", icon: "🔮" },
  stepback: { label: "Step-Back", color: "#f59e0b", icon: "⬆" },
};

const SOURCE_STYLE: Record<string, { label: string; color: string; icon: string }> = {
  vector: { label: "vector", color: "#3b82f6", icon: "📚" },
  sql: { label: "sql", color: "#10b981", icon: "🗄" },
  web: { label: "web", color: "#06b6d4", icon: "🌐" },
  graph: { label: "graph", color: "#ec4899", icon: "🔗" },
};

const DEGRADED_LABEL: Record<string, string> = {
  intent: "本次未识别意图",
  expansion: "本次未启用改写",
  decompose: "本次未拆解",
  disabled: "预处理已关闭",
};

export function IntentBadge({ intent, confidence, reason }: { intent: string; confidence?: number; reason?: string }) {
  const style = INTENT_STYLE[intent] ?? INTENT_STYLE.fact_qa;
  return (
    <span
      className="intent-badge"
      style={{ background: style.color }}
      title={reason ? `判定依据：${reason}` : undefined}
      aria-label={`意图：${style.label}${confidence != null ? `，置信度 ${Math.round(confidence * 100)}%` : ""}`}
    >
      {style.label}
      {confidence != null && confidence > 0 && <span className="intent-conf"> {Math.round(confidence * 100)}%</span>}
    </span>
  );
}

function SourceTag({ source }: { source: string }) {
  const style = SOURCE_STYLE[source] ?? SOURCE_STYLE.vector;
  return (
    <span
      className="route-tag"
      style={{ background: `color-mix(in srgb, ${style.color} 12%, transparent)`, color: style.color }}
      aria-label={`数据源 ${style.label}`}
    >
      {style.icon} {style.label}
    </span>
  );
}

export function QueryAnalysisPanel({ analysis }: { analysis: QueryAnalysis }) {
  return (
    <div className="analysis-panel" aria-label="Query 分析">
      <div className="analysis-row">
        <span className="analysis-label">意图识别</span>
        {analysis.degraded.includes("intent") ? (
          <span className="degrade-note" style={{ margin: 0 }}>{DEGRADED_LABEL.intent}</span>
        ) : (
          <>
            <IntentBadge intent={analysis.intent} confidence={analysis.intent_confidence} reason={analysis.intent_reason} />
            {analysis.needs_retrieval ? (
              <span className="route-tag route-vector">需检索</span>
            ) : (
              <span className="route-tag" style={{ background: "color-mix(in srgb, #64748b 12%, transparent)", color: "#64748b" }}>
                闲聊短路 · 不走检索
              </span>
            )}
          </>
        )}
      </div>

      <div className="analysis-row">
        <span className="analysis-label">查询改写</span>
        <div className="rewrite-list">
          {analysis.degraded.includes("expansion") && (
            <span className="degrade-note" style={{ margin: 0 }}>{DEGRADED_LABEL.expansion}</span>
          )}
          {analysis.rewrites.map((rw, i) => {
            const style = REWRITE_STYLE[rw.type] ?? REWRITE_STYLE.rewrite;
            return (
              <div key={i} className={`rewrite-item${rw.used ? "" : " unused"}`} title={`由 ${rw.expander} 生成${rw.skip_reason ? `；未使用：${rw.skip_reason}` : ""}`}>
                <span aria-hidden>{style.icon}</span>
                <span className="route-tag" style={{ background: `color-mix(in srgb, ${style.color} 12%, transparent)`, color: style.color }}>
                  {style.label}
                </span>
                <span className="rewrite-text">{rw.text}</span>
                {!rw.used && <span className="trace-id">（未使用）</span>}
              </div>
            );
          })}
        </div>
      </div>

      {analysis.sub_questions.length > 0 && (
        <div className="analysis-row">
          <span className="analysis-label">多跳拆解</span>
          <div className="rewrite-list">
            {analysis.degraded.includes("decompose") && (
              <span className="degrade-note" style={{ margin: 0 }}>{DEGRADED_LABEL.decompose}</span>
            )}
            {analysis.sub_questions.map((sq, i) => (
              <div key={i} className={`rewrite-item${sq.skipped ? " unused" : ""}`} title={sq.skip_reason || undefined}>
                <span className="trace-id">├─</span>
                <span className="rewrite-text">子问题 {i + 1}：{sq.text}</span>
                <SourceTag source={sq.expected_source} />
                {sq.skipped && <span className="trace-id">（已跳过）</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="analysis-row">
        <span className="analysis-label">数据源路由</span>
        {analysis.sources.length > 0 ? (
          analysis.sources.map((s) => <SourceTag key={s} source={s} />)
        ) : (
          <span className="trace-id">（短路直答，无检索）</span>
        )}
        <span className="trace-id" style={{ marginLeft: "auto" }}>
          预处理 {analysis.elapsed_ms}ms · {analysis.llm_calls} 次模型调用
        </span>
      </div>
    </div>
  );
}
