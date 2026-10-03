/** 报告详情页（阶段 8 前端规范 §3.2）：五标签页 + 实时进度轮询。
 *
 * - 最终报告 / Plan / Evidence / Critic / 元数据；
 * - 进行中每 1.5s 轮询（用户离开回来可见全过程）；
 * - 未通过时遗留问题清单顶部显著展示；未评审与未通过视觉区分；
 * - 首稿/终稿对比入口（评估评审机制价值的客观依据）。
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getReport } from "../../lib/api-client";
import type { ReportDetail } from "../../types";
import { BlackboardPanel } from "./components/BlackboardPanel";
import { CriticScoreHistory } from "./components/CriticScoreHistory";
import { RoleBadge } from "./components/RoleBadge";

const IN_PROGRESS = new Set(["planning", "researching", "validating", "writing", "critiquing"]);
const TABS = ["最终报告", "Plan", "Evidence", "Critic", "元数据"] as const;

const STATUS_TEXT: Record<string, string> = {
  planning: "规划中", researching: "检索中", validating: "校验中", writing: "撰写中",
  critiquing: "评审中", accepted: "已通过", rejected: "未通过", capped: "未通过（达轮次上限）",
  unreviewed: "未评审", failed: "失败",
};

export function ReportDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [report, setReport] = useState<ReportDetail | null>(null);
  const [tab, setTab] = useState<(typeof TABS)[number]>("最终报告");
  const [error, setError] = useState<string | null>(null);
  const [compareDraft, setCompareDraft] = useState<number | null>(null);

  const load = useCallback(async () => {
    if (!id) return;
    const envelope = await getReport(id);
    if (envelope.data) setReport(envelope.data);
    else setError(envelope.error?.message ?? "加载失败");
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!report || !IN_PROGRESS.has(report.status)) return;
    const timer = setInterval(() => void load(), 1500);
    return () => clearInterval(timer);
  }, [report, load]);

  if (error) return <div className="page"><div className="error-inline" role="alert">⚠ {error}</div></div>;
  if (!report) return <div className="page"><div className="trace-id" style={{ padding: 24 }}>加载中…</div></div>;

  const bb = report.blackboard;
  const finalDraft = report.drafts[report.drafts.length - 1];
  const firstDraft = report.drafts[0];
  const roles = report.role_calls ?? {};

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1000 }}>
        <div className="card">
          <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <Link to="/reports" className="btn-text">← 返回</Link>
            <h3 style={{ margin: 0 }}>{report.title}</h3>
            <span className={`status-tag st-${report.accepted === true ? "pass" : report.accepted === false ? "warn" : report.status === "failed" ? "fail" : "running"}`}>
              {STATUS_TEXT[report.status] ?? report.status}
            </span>
            {report.critic_score != null && (
              <span className="mono">评分 {report.critic_score}/10</span>
            )}
            <span className="trace-id">轮次 {report.rounds} · {report.layers} 层</span>
          </div>
          <div style={{ marginTop: 8, display: "flex", gap: 6, flexWrap: "wrap" }}>
            {(["planner", "researcher", "validator", "writer", "critic"] as const).map((role) =>
              roles[role] ? <RoleBadge key={role} role={role} count={roles[role]} /> : null,
            )}
          </div>
          {IN_PROGRESS.has(report.status) && (
            <div className="pending-note" style={{ marginTop: 8 }}>
              <span className="typing-dots">执行中</span>（页面自动刷新，可离开稍后回来查看）
            </div>
          )}
          {report.accepted === false && report.unresolved_issues.length > 0 && (
            <div className="degrade-note" style={{ marginTop: 8 }}>
              <strong>未通过的遗留问题：</strong>
              {report.unresolved_issues.map((issue, i) => <div key={i}>· {issue}</div>)}
            </div>
          )}
          {report.status === "unreviewed" && (
            <div className="pending-note" style={{ marginTop: 8 }}>
              报告已生成，但评审环节未生效（评审服务异常）。报告内容未经评审确认。
            </div>
          )}
          {report.error && (
            <div className="error-inline" role="alert" style={{ marginTop: 8 }}>⚠ {report.error}</div>
          )}
        </div>

        <div className="card">
          <div className="report-tabs" role="tablist">
            {TABS.map((t) => (
              <button
                key={t} type="button" role="tab" aria-selected={tab === t}
                className={`report-tab${tab === t ? " active" : ""}`}
                onClick={() => setTab(t)}
                onKeyDown={(e) => {
                  const idx = TABS.indexOf(tab);
                  if (e.key === "ArrowRight") setTab(TABS[(idx + 1) % TABS.length]);
                  if (e.key === "ArrowLeft") setTab(TABS[(idx - 1 + TABS.length) % TABS.length]);
                }}
              >
                {t}
              </button>
            ))}
          </div>

          {tab === "最终报告" && (
            <div>
              {report.drafts.length > 1 && (
                <div style={{ marginBottom: 8 }}>
                  <button
                    type="button" className="btn-text"
                    onClick={() => setCompareDraft(compareDraft === null ? 0 : null)}
                  >
                    {compareDraft === null ? "对比首稿/终稿" : "只看终稿"}
                  </button>
                </div>
              )}
              {compareDraft !== null && firstDraft ? (
                <div className="draft-compare">
                  <div>
                    <div className="trace-id">首稿（第 {firstDraft.round} 轮 · {firstDraft.word_count} 字）</div>
                    <div className="answer-text">{firstDraft.content}</div>
                  </div>
                  <div>
                    <div className="trace-id">终稿（第 {finalDraft?.round} 轮 · {finalDraft?.word_count} 字）</div>
                    <div className="answer-text">{finalDraft?.content}</div>
                  </div>
                </div>
              ) : (
                <div className="answer-text" style={{ whiteSpace: "pre-wrap" }}>
                  {finalDraft?.content || (IN_PROGRESS.has(report.status) ? "撰写中…" : "（无内容）")}
                </div>
              )}
            </div>
          )}

          {tab === "Plan" && bb && (
            <div>
              {Array.from({ length: report.layers }, (_, layer) => (
                <div key={layer} className="plan-layer">
                  <div className="trace-id">第 {layer + 1} 层{layer < report.layers - 1 ? " →" : ""}</div>
                  {bb.plan.filter((t) => t.layer === layer).map((t) => (
                    <div key={t.task_id} className={`bb-task status-${t.status}`}>
                      <span className="mono">{t.task_id}</span>
                      <span className="bb-tool mono">[{t.tool_name}]</span>
                      <span className="bb-desc">{t.description}</span>
                      {t.depends_on.length > 0 && (
                        <span className="trace-id">依赖: {t.depends_on.join(", ")}</span>
                      )}
                    </div>
                  ))}
                </div>
              ))}
              {bb.plan.length === 0 && <div className="trace-id">规划中…</div>}
            </div>
          )}

          {tab === "Evidence" && bb && (
            <BlackboardPanel plan={[]} evidence={bb.evidence} critiques={[]} />
          )}

          {tab === "Critic" && bb && (
            <CriticScoreHistory
              rounds={bb.critiques}
              drafts={report.drafts}
              accepted={report.accepted}
              finalScore={report.critic_score}
            />
          )}

          {tab === "元数据" && (
            <dl className="entity-detail">
              <dt>目标</dt><dd>{report.goal}</dd>
              <dt>报告 ID</dt><dd className="mono">{report.report_id}</dd>
              <dt>状态</dt><dd>{STATUS_TEXT[report.status]}（accepted={String(report.accepted)}）</dd>
              <dt>角色调用次数</dt>
              <dd className="mono">{JSON.stringify(report.role_calls)}</dd>
              <dt>创建时间</dt>
              <dd>{new Date(report.created_at * 1000).toLocaleString("zh-CN", { hour12: false })}</dd>
              <dt>更新时间</dt>
              <dd>{new Date(report.updated_at * 1000).toLocaleString("zh-CN", { hour12: false })}</dd>
            </dl>
          )}
        </div>
      </div>
    </div>
  );
}
