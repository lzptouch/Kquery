/** 子问题列表（）。
 *
 * 多跳拆解的价值在于"每个子问题各自检索、各自拿证据"。只列文本的话，
 * 用户无法把答案的某一段对应回某个子问题——所以点击子问题要能定位到它的证据，
 * 与 QueryRewriteDiff 的高亮共用同一套定位通道。
 */
import { SourceTag } from "./SourceTag";
import type { QueryAnalysis } from "../../../types";

export function SubQuestionList({
  subQuestions,
  degraded,
  focusedIndex,
  onFocus,
}: {
  subQuestions: QueryAnalysis["sub_questions"];
  degraded: string[];
  focusedIndex?: number | null;
  onFocus?: (index: number | null) => void;
}) {
  if (degraded.includes("decompose") && subQuestions.length === 0) {
    return <span className="degrade-note" style={{ margin: 0 }}>本次未拆解</span>;
  }

  return (
    <div className="rewrite-list">
      {subQuestions.map((sq, i) => {
        const isFocused = focusedIndex === i;
        return (
          <div
            key={i}
            className={`rewrite-item${sq.skipped ? " unused" : ""}${isFocused ? " focused" : ""}`}
            title={sq.skip_reason || undefined}
          >
            <span className="trace-id" aria-hidden>├─</span>
            <span className="rewrite-text">子问题 {i + 1}：{sq.text}</span>
            <SourceTag source={sq.expected_source} />
            {sq.skipped && <span className="trace-id">（已跳过）</span>}
            {!sq.skipped && onFocus && (
              <button
                type="button"
                className="btn-text"
                onClick={() => onFocus(isFocused ? null : i)}
                aria-pressed={isFocused}
              >
                {isFocused ? "取消高亮" : "看命中证据"}
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}