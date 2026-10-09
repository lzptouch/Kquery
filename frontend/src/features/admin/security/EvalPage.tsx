/** 评测平台（阶段 9 前端规范 §六）：评测集 + 运行 + 结果对比。
 *
 * 每次运行绑定评测集版本 + 模型版本（缺一结论不可信）。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchEvalDatasets, fetchEvalRun, fetchEvalRuns, runEval, saveEvalDataset } from "../../../lib/api-client";
import { EvalComparisonChart } from "./EvalComparisonChart";
import type { EvalDataset, EvalRunDetail, EvalRunSummary } from "../../../types";

export function EvalPage() {
  const [datasets, setDatasets] = useState<EvalDataset[]>([]);
  const [runs, setRuns] = useState<EvalRunSummary[]>([]);
  const [selected, setSelected] = useState<EvalRunDetail | null>(null);
  const [running, setRunning] = useState(false);
  const [selectedDataset, setSelectedDataset] = useState("");
  const [error, setError] = useState<string | null>(null);
  // 对比基线：勾一个历史运行作为 A，当前选中的运行作为 B
  const [baselineRunId, setBaselineRunId] = useState<string>("");

  const load = useCallback(async () => {
    const [ds, rs] = await Promise.all([fetchEvalDatasets(), fetchEvalRuns()]);
    if (ds.data) {
      setDatasets(ds.data);
      if (!selectedDataset && ds.data.length) {
        setSelectedDataset(`${ds.data[0].name}@${ds.data[0].version}`);
      }
    }
    if (rs.data) setRuns(rs.data);
  }, [selectedDataset]);

  useEffect(() => {
    void load();
  }, [load]);

  const run = async () => {
    if (!selectedDataset) return;
    const [name, version] = selectedDataset.split("@");
    setRunning(true);
    setError(null);
    try {
      const envelope = await runEval(name, version);
      if (envelope.data) {
        void load();
        const detail = await fetchEvalRun(envelope.data.run_id);
        if (detail.data) setSelected(detail.data);
      } else {
        setError(envelope.error?.message ?? "评测失败");
      }
    } finally {
      setRunning(false);
    }
  };

  const seedDemo = async () => {
    await saveEvalDataset("demo", "v1", [
      { question: "什么是机会成本？", gold_answer: "为了得到某种东西所放弃的东西", gold_evidence: ["机会成本"], should_refuse: false },
      { question: "货币市场工具有哪些？", gold_answer: "国库券、大额存单、商业票据等", gold_evidence: ["货币市场"], should_refuse: false },
      { question: "今天的股价是多少？", gold_answer: "", gold_evidence: [], should_refuse: true },
    ]);
    void load();
  };

  const metricCell = (v: number | null) =>
    v == null ? "-" : (
      <span className={`mono ${v >= 0.85 ? "score-pass" : v >= 0.6 ? "score-warn" : "score-fail"}`}>
        {v.toFixed(2)}
      </span>
    );

  return (
    <div className="page">
      <div className="page-inner">
        <div className="card">
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <h3 style={{ margin: 0 }}>评测平台</h3>
            <select className="probe-input" value={selectedDataset} onChange={(e) => setSelectedDataset(e.target.value)} aria-label="评测集">
              {datasets.map((d) => (
                <option key={`${d.name}@${d.version}`} value={`${d.name}@${d.version}`}>
                  {d.name}@{d.version}（{d.count} 样本）
                </option>
              ))}
            </select>
            <button type="button" className="btn primary" disabled={running || !selectedDataset} onClick={() => void run()}>
              {running ? "评测中…" : "运行评测"}
            </button>
            {datasets.length === 0 && (
              <button type="button" className="btn-text" onClick={() => void seedDemo()}>
                生成演示评测集
              </button>
            )}
          </div>
          {error && <div className="error-inline" role="alert" style={{ marginTop: 8 }}>⚠ {error}</div>}
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>历史运行</h3>
          {runs.length === 0 ? (
            <div className="empty-state" style={{ padding: 24 }}>暂无评测记录</div>
          ) : (
            <>
            <table className="files-table">
              <thead>
                <tr>
                  <th scope="col">基线</th>
                  <th scope="col">评测集</th>
                  <th scope="col">模型版本</th>
                  <th scope="col">代码版本</th>
                  <th scope="col">样本</th>
                  <th scope="col">Faithfulness</th>
                  <th scope="col">Citation</th>
                  <th scope="col">Refusal</th>
                  <th scope="col">Coverage</th>
                  <th scope="col">时间</th>
                  <th scope="col"></th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.run_id}>
                    <td>
                      <input
                        type="radio"
                        name="eval-baseline"
                        checked={baselineRunId === r.run_id}
                        onChange={() => setBaselineRunId(r.run_id)}
                        aria-label={`以 ${r.run_id} 作为对比基线`}
                      />
                    </td>
                    <td>{r.dataset}@{r.dataset_version}</td>
                    <td className="mono">{r.model_version}</td>
                    <td className="mono">{r.code_version ?? "-"}</td>
                    <td className="mono">{r.sample_count}</td>
                    <td>{metricCell(r.faithfulness)}</td>
                    <td>{metricCell(r.citation_accuracy)}</td>
                    <td>{metricCell(r.refusal_accuracy)}</td>
                    <td>{metricCell(r.coverage ?? null)}</td>
                    <td className="trace-id">{new Date(r.created_at * 1000).toLocaleString("zh-CN", { hour12: false })}</td>
                    <td>
                      <button type="button" className="btn-text" onClick={() => void fetchEvalRun(r.run_id).then((e) => e.data && setSelected(e.data))}>
                        详情
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted" style={{ fontSize: 13 }}>
              勾选一行作为对比基线，再点另一行的「详情」，下方「版本对比」会给出
              逐指标差值与新增失败 / 已修复样本清单。
            </p>
            </>
          )}
        </div>

        <EvalComparisonChart runIdA={baselineRunId} runIdB={selected?.run_id ?? ""} />

        {selected && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>
              运行详情：{selected.dataset}@{selected.dataset_version} · {selected.model_version}
            </h3>
            <div className="kpi-row" style={{ marginBottom: 12 }}>
              <span className="kpi-item">Faithfulness <strong className="mono">{selected.faithfulness.toFixed(2)}</strong></span>
              <span className="kpi-item">Citation <strong className="mono">{selected.citation_accuracy.toFixed(2)}</strong></span>
              <span className="kpi-item">Refusal <strong className="mono">{selected.refusal_accuracy?.toFixed(2) ?? "-"}</strong></span>
              <span className="kpi-item">样本 <strong className="mono">{selected.sample_count}</strong></span>
            </div>
            <table className="files-table">
              <thead>
                <tr><th>#</th><th>问题</th><th>判定</th><th>Faithful</th><th>Citation</th><th>应拒答</th></tr>
              </thead>
              <tbody>
                {selected.details.map((d) => (
                  <tr key={d.idx}>
                    <td className="mono">{d.idx}</td>
                    <td>{d.question}</td>
                    <td><span className={`status-tag ${d.verdict === "answered" ? "st-pass" : "st-warn"}`}>{d.verdict}</span></td>
                    <td className="mono">{d.faithfulness.toFixed(2)}</td>
                    <td className="mono">{d.citation.toFixed(2)}</td>
                    <td>{d.should_refuse ? (d.refuse_ok ? "✓ 已拒" : "✗ 未拒") : "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
