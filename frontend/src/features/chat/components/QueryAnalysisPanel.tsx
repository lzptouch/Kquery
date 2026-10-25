/** Query 分析详情面板。
 *
 * 四段展示：意图（IntentBadge）/ 改写（QueryRewriteDiff）/ 子问题（SubQuestionList）/ 数据源路由。
 * 降级如实标注（哪一步未生效），提示强度克制（面板内标注，不弹窗）。
 *
 * 三个子组件此前都内联在这个文件里，各自的交互（长文本展开、点击定位证据）
 * 因此无处安放。现在拆成独立文件，面板只负责组装与把"焦点"往上抛。
 */
import { useState } from "react";
import { QueryRewriteDiff } from "./QueryRewriteDiff";
import { SourceTag } from "./SourceTag";
import { SubQuestionList } from "./SubQuestionList";
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

export function QueryAnalysisPanel({
  analysis,
  onFocusSource,
}: {
  analysis: QueryAnalysis;
  /** 焦点回抛：父级据此高亮证据（改写↔证据、子问题↔证据的双向定位通道） */
  onFocusSource?: (focus: { kind: "rewrite" | "subquestion"; index: number } | null) => void;
}) {
  const [focus, setFocus] = useState<{ kind: "rewrite" | "subquestion"; index: number } | null>(null);

  const apply = (next: { kind: "rewrite" | "subquestion"; index: number } | null) => {
    setFocus(next);
    onFocusSource?.(next);
  };

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
        <QueryRewriteDiff
          rewrites={analysis.rewrites}
          degraded={analysis.degraded}
          onFocusRewrite={(i) => apply(i === null ? null : { kind: "rewrite", index: i })}
        />
      </div>

      {(analysis.sub_questions.length > 0 || analysis.degraded.includes("decompose")) && (
        <div className="analysis-row">
          <span className="analysis-label">多跳拆解</span>
          <SubQuestionList
            subQuestions={analysis.sub_questions}
            degraded={analysis.degraded}
            focusedIndex={focus?.kind === "subquestion" ? focus.index : null}
            onFocus={(i) => apply(i === null ? null : { kind: "subquestion", index: i })}
          />
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
          {analysis.cost != null && ` · 约 ${analysis.cost.toFixed(4)} 元`}
        </span>
      </div>
    </div>
  );
}