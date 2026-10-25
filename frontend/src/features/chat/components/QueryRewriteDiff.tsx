/** 查询改写差异（）。
 *
 * 此前改写列表内联在 QueryAnalysisPanel 里，缺两件文档明确要求的事：
 * 1. **长改写可展开**：HyDE 生成的是一段伪答案，不是短查询，默认全量展示会
 *    把面板撑到十几行，用户根本看不到原始问题；
 * 2. **点击改写高亮它命中的证据**：这是整个 最有价值的交互——
 *    让用户亲眼看到"这条改写换来了哪几条证据"，否则改写仍是黑盒。
 */
import { useState } from "react";
import type { QueryAnalysis } from "../../../types";

const REWRITE_STYLE: Record<string, { label: string; color: string; icon: string }> = {
  original: { label: "原始", color: "#64748b", icon: "💬" },
  rewrite: { label: "改写", color: "#3b82f6", icon: "✏️" },
  hyde: { label: "HyDE", color: "#8b5cf6", icon: "🔮" },
  stepback: { label: "Step-Back", color: "#f59e0b", icon: "⬆" },
};

const PREVIEW_LEN = 60;

export function QueryRewriteDiff({
  rewrites,
  degraded,
  onFocusRewrite,
}: {
  rewrites: QueryAnalysis["rewrites"];
  degraded: string[];
  /** 选中某条改写（回传给父级，由父级决定高亮哪批证据） */
  onFocusRewrite?: (index: number | null) => void;
}) {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const [focused, setFocused] = useState<number | null>(null);

  const toggleFocus = (index: number) => {
    const next = focused === index ? null : index;
    setFocused(next);
    onFocusRewrite?.(next);
  };

  if (degraded.includes("expansion") && rewrites.length === 0) {
    return <span className="degrade-note" style={{ margin: 0 }}>本次未启用改写</span>;
  }

  return (
    <div className="rewrite-list">
      {rewrites.map((rw, i) => {
        const style = REWRITE_STYLE[rw.type] ?? REWRITE_STYLE.rewrite;
        const isLong = rw.text.length > PREVIEW_LEN;
        const open = expanded[i] ?? false;
        const isFocused = focused === i;
        return (
          <div
            key={i}
            className={`rewrite-item${rw.used ? "" : " unused"}${isFocused ? " focused" : ""}`}
            title={`由 ${rw.expander} 生成${rw.skip_reason ? `；未使用：${rw.skip_reason}` : ""}`}
          >
            <span aria-hidden>{style.icon}</span>
            <span
              className="route-tag"
              style={{ background: `color-mix(in srgb, ${style.color} 12%, transparent)`, color: style.color }}
            >
              {style.label}
            </span>
            <span className="rewrite-text" data-expanded={open}>
              {open || !isLong ? rw.text : `${rw.text.slice(0, PREVIEW_LEN)}…`}
            </span>
            {!rw.used && <span className="trace-id">（未使用）</span>}
            <span className="rewrite-actions">
              {isLong && (
                <button
                  type="button"
                  className="btn-text"
                  onClick={() => setExpanded((prev) => ({ ...prev, [i]: !open }))}
                  aria-expanded={open}
                >
                  {open ? "收起" : "展开全文"}
                </button>
              )}
              <button
                type="button"
                className="btn-text"
                onClick={() => toggleFocus(i)}
                aria-pressed={isFocused}
                title={isFocused ? "取消高亮" : "高亮这条改写命中的证据"}
              >
                {isFocused ? "取消高亮" : "看命中证据"}
              </button>
            </span>
          </div>
        );
      })}
    </div>
  );
}