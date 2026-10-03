/** Blackboard 状态面板（阶段 8 前端规范 §2.2）。
 *
 * 三段：计划（含层与状态）/ 证据（采纳 ✓ 绿 / 淘汰 ✗ 灰删除线 + 原因 / 未校验 ？）/
 * 评审反馈（逐轮问题清单）。
 * 淘汰证据保留展示（不能直接隐藏——排查检索质量的直接线索）。
 */
import type { CritiqueRound, EvidenceRecordView, PlanTask } from "../../../types";

const TASK_STATUS_LABEL: Record<string, string> = {
  pending: "待执行", running: "执行中", done: "✓ 完成", failed: "✗ 失败",
};

export function BlackboardPanel({
  plan,
  evidence,
  critiques,
}: {
  plan: PlanTask[];
  evidence: EvidenceRecordView[];
  critiques: CritiqueRound[];
}) {
  const adopted = evidence.filter((e) => e.adopted === true).length;
  return (
    <div className="bb-panel">
      <div className="bb-section">
        <div className="bb-title">📋 Plan（{plan.length} 个子任务）</div>
        {plan.map((t) => (
          <div key={t.task_id} className={`bb-task status-${t.status}`}>
            <span className="mono">{t.task_id}</span>
            <span className="bb-layer">L{t.layer}</span>
            <span className="bb-tool mono">[{t.tool_name}]</span>
            <span className="bb-desc">{t.description}</span>
            <span className={`trace-id ${t.status === "failed" ? "bb-failed" : ""}`}>
              {TASK_STATUS_LABEL[t.status]}
              {t.failure_reason && <span title={t.failure_reason}>（{t.failure_reason.slice(0, 20)}）</span>}
            </span>
          </div>
        ))}
      </div>

      <div className="bb-section">
        <div className="bb-title">
          🔎 Evidence（{evidence.length} 条 / {adopted} 已采纳）
        </div>
        <dl className="bb-evidence-list">
          {evidence.map((e) => (
            <div
              key={e.evidence_id}
              className={`bb-evidence ${e.adopted === true ? "adopted" : e.adopted === false ? "rejected" : ""}`}
            >
              <dt className="mono">
                {e.evidence_id} · {e.task_id}
                {e.used_by_tasks.length > 0 && <span title="被这些任务复用">（复用→{e.used_by_tasks.join(",")}）</span>}
              </dt>
              <dd>
                <span className="bb-ev-content">{e.content.slice(0, 80)}{e.content.length > 80 ? "…" : ""}</span>
                {e.adopted === true && <span className="bb-adopted">✓ {e.score?.toFixed(2)}</span>}
                {e.adopted === false && (
                  <span className="bb-rejected" title={e.rejected_reason}>✗ {e.rejected_reason.slice(0, 24)}</span>
                )}
                {e.adopted === null && <span className="trace-id">? 未校验</span>}
              </dd>
            </div>
          ))}
          {evidence.length === 0 && <div className="trace-id">暂无证据</div>}
        </dl>
      </div>

      {critiques.length > 0 && (
        <div className="bb-section">
          <div className="bb-title">🔍 Feedback</div>
          {critiques.map((c) => (
            <div key={c.round} className="bb-critique">
              <span className="mono">R{c.round}</span> 评分 {c.score}/10
              {c.issues.map((issue, i) => (
                <div key={i} className="bb-issue">· {issue}</div>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
