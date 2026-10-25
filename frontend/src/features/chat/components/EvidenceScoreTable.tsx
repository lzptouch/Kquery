/** 证据评分表（）。
 *
 * 此前内联在 EvidenceCard 里，缺两处文档明确点名的东西：
 * 1. **高/中/低等级文字**：只有分数没有等级，用户得自己判断 0.62 算高还是算中；
 * 2. **可访问性**：等级不能只靠颜色表达（），每行同时给文字标签。
 */
import type { Evidence } from "../../../types";

type Grade = "high" | "medium" | "low" | "none";

const GRADE: Record<Grade, { label: string; color: string }> = {
  high: { label: "高", color: "#16a34a" },
  medium: { label: "中", color: "#f59e0b" },
  low: { label: "低", color: "#d97706" },
  none: { label: "未评分", color: "#64748b" },
};

/** 分档阈值：≥0.7 高、≥0.4 中、其余低。低于 0.4 的证据基本不该进答案，
 *  单列出来是为了让用户看到"系统自己也知道它不可靠"。 */
export function gradeOf(score: number | null | undefined): Grade {
  if (score === null || score === undefined) return "none";
  if (score >= 0.7) return "high";
  if (score >= 0.4) return "medium";
  return "low";
}

export function EvidenceScoreTable({
  evidence,
  adoptedThreshold,
  onFocusChunk,
}: {
  evidence: Evidence[];
  /** 采纳阈值：低于它的证据被 Validator 淘汰，面板要标出来 */
  adoptedThreshold?: number;
  onFocusChunk?: (chunkId: string) => void;
}) {
  if (evidence.length === 0) return null;
  const rows = evidence.map((ev) => ({
    ev,
    grade: gradeOf(ev.score),
    adopted: adoptedThreshold === undefined ? true : (ev.score ?? 0) >= adoptedThreshold,
  }));
  const adoptedCount = rows.filter((r) => r.adopted).length;

  return (
    <section className="evidence-score" aria-label="证据相关度评分">
      <table className="files-table score-table">
        <caption className="sr-only">
          {evidence.length} 条证据的相关度评分，{adoptedCount} 条达到采纳阈值
          {adoptedThreshold !== undefined ? ` ${adoptedThreshold}` : ""}
        </caption>
        <thead>
          <tr>
            <th scope="col">#</th>
            <th scope="col">相关度</th>
            <th scope="col">等级</th>
            <th scope="col">来源</th>
            <th scope="col">是否采纳</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(({ ev, grade, adopted }) => {
            const g = GRADE[grade];
            return (
              <tr
                key={ev.chunk_id}
                className={adopted ? "" : "score-rejected"}
                onClick={onFocusChunk ? () => onFocusChunk(ev.chunk_id) : undefined}
                style={onFocusChunk ? { cursor: "pointer" } : undefined}
              >
                <td className="mono">#{ev.rank}</td>
                <td>
                  <span
                    className="score-bar"
                    style={{
                      width: `${Math.round((ev.score ?? 0) * 100)}%`,
                      background: g.color,
                    }}
                    aria-hidden
                  />
                  <span className="mono" style={{ marginLeft: 6 }}>
                    {ev.score === null || ev.score === undefined ? "-" : ev.score.toFixed(3)}
                  </span>
                </td>
                {/* 颜色 + 文字双通道：不能只用色块表达等级 */}
                <td style={{ color: g.color }} aria-label={`等级 ${g.label}`}>
                  <span aria-hidden>{grade === "high" ? "●" : grade === "medium" ? "◐" : grade === "low" ? "○" : "–"}</span>{" "}
                  {g.label}
                </td>
                <td className="trace-id">{ev.source}</td>
                <td>
                  <span aria-hidden>{adopted ? "✓" : "✗"}</span>{" "}
                  {adopted ? "采纳" : "淘汰"}
                  {adoptedThreshold !== undefined && !adopted && (
                    <span className="trace-id">（低于 {adoptedThreshold}）</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}