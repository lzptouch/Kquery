/** 报告中心（阶段 8 前端规范 §3.1）：列表 + 新建。
 *
 * 异步任务语义：提交后立即返回，列表轮询刷新进行中任务的状态。
 * 三种终态视觉可区分：已通过（绿）/ 未通过（橙，不是错误）/ 未评审（灰）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { DataState } from "../../components/DataState";
import { Link, useNavigate } from "react-router-dom";
import { createReport, deleteReport, listReports } from "../../lib/api-client";
import type { ReportStatus, ReportSummary } from "../../types";

const STATUS_META: Record<ReportStatus, { label: string; className: string }> = {
  planning: { label: "规划中", className: "st-pending" },
  researching: { label: "检索中", className: "st-running" },
  validating: { label: "校验中", className: "st-running" },
  writing: { label: "撰写中", className: "st-running" },
  critiquing: { label: "评审中", className: "st-running" },
  accepted: { label: "✓ 已通过", className: "st-pass" },
  rejected: { label: "✗ 未通过", className: "st-warn" },
  capped: { label: "⚠ 未通过（达上限）", className: "st-warn" },
  unreviewed: { label: "未评审", className: "st-none" },
  failed: { label: "✗ 失败", className: "st-fail" },
};

const IN_PROGRESS = new Set(["planning", "researching", "validating", "writing", "critiquing"]);

export function ReportsPage() {
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [goal, setGoal] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const navigate = useNavigate();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const envelope = await listReports();
      if (envelope.data) setReports(envelope.data);
      else if (envelope.error?.message) setError(envelope.error.message);
    } catch (err) {
      setError((err as Error).message || "报告列表加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  // 轮询条件要读最新的 reports，但不能把它放进依赖：
  // setReports 每次都产生新数组引用，一旦进依赖就会
  // load() → setReports(新数组) → effect 重跑 → load() …形成死循环，
  // 页面永远停在"加载中"，同时每秒打两次请求把日志刷爆。
  // 用 ref 读最新值，effect 只依赖 load。
  const reportsRef = useRef(reports);
  reportsRef.current = reports;

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    // 有进行中任务时 1.5s 轮询（异步任务：离开后回来可见最新状态）
    const timer = setInterval(() => {
      if (reportsRef.current.some((r) => IN_PROGRESS.has(r.status))) void load();
    }, 1500);
    return () => clearInterval(timer);
  }, [load]);

  const submit = useCallback(async () => {
    if (goal.trim().length < 5) {
      setError("目标描述太短——请给出具体目标，例如：「分析曼昆的机会成本概念与索维尔的价格理论的关系」");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const envelope = await createReport(goal.trim());
      if (envelope.data) {
        setGoal("");
        navigate(`/reports/${envelope.data.report_id}`);
      } else {
        setError(envelope.error?.message ?? "创建失败");
      }
    } finally {
      setSubmitting(false);
    }
  }, [goal, navigate]);

  return (
    <div className="page">
      <div className="page-inner">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>新建深度报告</h3>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              className="probe-input"
              style={{ flex: 1 }}
              placeholder="描述报告目标，多智能体将自动拆解、检索、撰写、评审…"
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void submit()}
              aria-label="报告目标"
            />
            <button type="button" className="btn primary" disabled={submitting} onClick={() => void submit()}>
              {submitting ? "提交中…" : "新建报告"}
            </button>
          </div>
          {error && <div className="error-inline" role="alert" style={{ marginTop: 8 }}>⚠ {error}</div>}
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>报告列表</h3>
          <DataState
            loading={loading}
            error={error}
            empty={reports.length === 0}
            emptyText="暂无报告，先创建一个"
            emptyHint="输入目标描述后点击「生成报告」，生成过程可实时查看进度。"
            onRetry={() => void load()}
          >
          {reports.length === 0 ? (
            <div className="empty-state" style={{ padding: 24 }}>暂无报告，先创建一个</div>
          ) : (
            <table className="files-table">
              <thead>
                <tr>
                  <th>报告</th><th>状态</th><th>评分</th><th>轮次</th><th>层数</th><th>创建时间</th><th></th>
                </tr>
              </thead>
              <tbody>
                {reports.map((r) => {
                  const meta = STATUS_META[r.status] ?? STATUS_META.failed;
                  return (
                    <tr key={r.report_id}>
                      <td>
                        <Link to={`/reports/${r.report_id}`} className="report-link">{r.title}</Link>
                      </td>
                      <td><span className={`status-tag ${meta.className}`}>{meta.label}</span></td>
                      <td className="mono">{r.critic_score != null ? `${r.critic_score}/10` : "-"}</td>
                      <td className="mono">{r.rounds || "-"}</td>
                      <td className="mono">{r.layers || "-"}</td>
                      <td className="trace-id">{new Date(r.created_at * 1000).toLocaleString("zh-CN", { hour12: false })}</td>
                      <td>
                        <button
                          type="button"
                          className="btn-text danger"
                          onClick={() => {
                            if (window.confirm(`删除报告「${r.title}」？草稿与评审记录将一并删除。`)) {
                              void deleteReport(r.report_id).then(() => load());
                            }
                          }}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          </DataState>
        </div>
      </div>
    </div>
  );
}
