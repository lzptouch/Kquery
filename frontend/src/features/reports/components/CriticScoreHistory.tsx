/** Critic 评分历史（阶段 8 前端规范 §2.3 / §8.2）。
 *
 * 评分色板：8-10 绿（通过）/ 5-7 黄（待改进）/ 0-4 红（不合格）/ 无评分 灰（未评审）。
 * 每轮独立区块：评分 + 问题清单 + 已解决问题（对照观察"评审是否在收敛"）。
 */
import type { CritiqueRound, Draft } from "../../../types";

function scoreClass(score: number | null): string {
  if (score == null) return "score-none";
  if (score >= 8) return "score-pass";
  if (score >= 5) return "score-warn";
  return "score-fail";
}

export function CriticScoreHistory({
  rounds,
  drafts,
  accepted,
  finalScore,
}: {
  rounds: CritiqueRound[];
  drafts: Draft[];
  accepted: boolean | null;
  finalScore: number | null;
}) {
  return (
    <div className="critic-history">
      {rounds.map((c) => {
        const draft = drafts.find((d) => d.round === c.round);
        return (
          <section key={c.round} className="critic-round" aria-label={`第 ${c.round} 轮评审`}>
            <header className="critic-round-head">
              <strong>第 {c.round} 轮</strong>
              <span className={`critic-score ${scoreClass(c.score)}`}>
                {c.score}/10 {c.score >= 8 ? "✅ 通过" : c.score >= 5 ? "⚠ 待改进" : "❌ 不合格"}
              </span>
              {draft && <span className="trace-id mono">{draft.word_count} 字</span>}
            </header>
            {c.issues.length > 0 && (
              <div className="critic-issues">
                问题：
                {c.issues.map((issue, i) => (
                  <span key={i} className="issue-tag">{issue}</span>
                ))}
              </div>
            )}
            {c.resolved_issues.length > 0 && (
              <div className="critic-resolved">
                改进：
                {c.resolved_issues.map((issue, i) => (
                  <span key={i} className="issue-tag resolved">{issue}</span>
                ))}
              </div>
            )}
          </section>
        );
      })}
      {rounds.length === 0 && (
        <div className="trace-id">
          {accepted === null ? "未评审（评审环节未执行或异常）" : "暂无评审记录"}
        </div>
      )}
      {finalScore != null && (
        <div className="critic-final">
          最终评分：<strong className={scoreClass(finalScore)}>{finalScore}/10</strong>
          {accepted === false && <span className="fuse-badge" style={{ marginLeft: 8 }}>⚠ 未通过（已达轮次上限）</span>}
        </div>
      )}
    </div>
  );
}
