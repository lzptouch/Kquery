/** 评测对比图（stage-9 §7.4）。
 *
 * 此前只能看单次运行的四个 KPI，"改了 prompt 之后是变好还是变坏"这件事
 * 要靠人肉比对两张图——而均值没动但错误类型换了的情况，肉眼恰恰看不出来。
 *
 * 所以这里不只画指标差值，还要给出"本次新增的失败样本"与"本次修好的样本"
 * 两张清单：调优真正要看的不是数字，是数字背后的那几条。
 */
import { useCallback, useEffect, useState } from "react";
import { authHeaders } from "../../../lib/auth";

export interface EvalMetricDelta {
  metric: string;
  value_a: number | null;
  value_b: number | null;
  delta: number | null;
  delta_pct: number | null;
}

export interface EvalSampleDiff {
  idx: number;
  question: string;
  reasons: string[];
}

export interface EvalCompare {
  a: Record<string, unknown> & { run_id: string; code_version?: string; model_version?: string };
  b: Record<string, unknown> & { run_id: string; code_version?: string; model_version?: string };
  same_dataset: boolean;
  comparable: boolean;
  note: string;
  metrics: EvalMetricDelta[];
  regressions: EvalSampleDiff[];
  fixed: EvalSampleDiff[];
  regression_count: number;
  fixed_count: number;
}

const METRIC_LABEL: Record<string, string> = {
  faithfulness: "忠实度",
  citation_accuracy: "引用准确率",
  refusal_accuracy: "拒答准确率",
  coverage: "期望证据覆盖率",
};

function fmt(value: number | null | undefined, digits = 3): string {
  return value === null || value === undefined ? "-" : value.toFixed(digits);
}

/** 变化方向：>0 变好（四个指标都是越高越好）。 */
function direction(delta: number | null): "up" | "down" | "flat" {
  if (delta === null || Math.abs(delta) < 1e-9) return "flat";
  return delta > 0 ? "up" : "down";
}

export function EvalComparisonChart({
  runIdA,
  runIdB,
}: {
  runIdA: string;
  runIdB: string;
}) {
  const [data, setData] = useState<EvalCompare | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    if (!runIdA || !runIdB || runIdA === runIdB) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(
        `/admin/eval/compare?a=${encodeURIComponent(runIdA)}&b=${encodeURIComponent(runIdB)}`,
        // TASK-160：/admin/* 需要管理员身份；裸 fetch 会 403（浏览器 E2E 抓到过）
        { headers: authHeaders() },
      );
      const body = await resp.json();
      if (body?.status === "ok" && body.data) setData(body.data);
      else setError(body?.error?.message ?? "对比失败");
    } catch {
      setError("对比失败，请检查网络");
    } finally {
      setLoading(false);
    }
  }, [runIdA, runIdB]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!runIdA || !runIdB) {
    return (
      <section className="panel">
        <h3>版本对比</h3>
        <p className="muted">选择两个运行记录后可查看逐指标对比。</p>
      </section>
    );
  }
  if (runIdA === runIdB) {
    return (
      <section className="panel">
        <h3>版本对比</h3>
        <p className="muted">请选择两个不同的运行记录。</p>
      </section>
    );
  }
  if (loading) {
    return (
      <div className="panel" role="status" aria-live="polite">
        正在对比两次运行…
      </div>
    );
  }
  if (error || !data) {
    return (
      <div className="panel" role="alert">
        {error ?? "对比数据不可用"}
      </div>
    );
  }

  const maxAbs = Math.max(
    0.01,
    ...data.metrics.map((m) => Math.abs(m.value_b ?? 0)),
  );

  return (
    <section className="panel" aria-labelledby="eval-compare-title">
      <h3 id="eval-compare-title">版本对比</h3>
      <p className="muted" style={{ fontSize: 13 }}>
        基线 <code className="mono">{data.a.code_version || data.a.run_id}</code>
        {" → "}
        本次 <code className="mono">{data.b.code_version || data.b.run_id}</code>
        {"  "}
        模型 <code className="mono">{data.b.model_version || "-"}</code>
      </p>

      {!data.comparable && (
        <div className="degrade-note" role="note">
          ⚠ {data.note}
        </div>
      )}

      {/* 逐指标：条形对比 + 差值 + 方向文字（三重编码，不只靠颜色） */}
      <div className="cmp-metrics">
        {data.metrics.map((m) => {
          const dir = direction(m.delta);
          const basePct = ((m.value_a ?? 0) / maxAbs) * 100;
          const nextPct = ((m.value_b ?? 0) / maxAbs) * 100;
          return (
            <div key={m.metric} className="cmp-metric">
              <div className="cmp-metric-name">
                {METRIC_LABEL[m.metric] ?? m.metric}
                <span className="mono cmp-delta" data-dir={dir}>
                  {m.delta === null ? "无可比数据" : (
                    <>
                      {dir === "up" ? "↑" : dir === "down" ? "↓" : "→"}{" "}
                      {m.delta >= 0 ? "+" : ""}{fmt(m.delta)}
                      {m.delta_pct !== null && (
                        <span className="muted">（{m.delta_pct >= 0 ? "+" : ""}{m.delta_pct}%）</span>
                      )}
                    </>
                  )}
                </span>
              </div>
              <div className="cmp-bar-row" title={`基线 ${fmt(m.value_a)}`}>
                <span className="cmp-bar-label">基线</span>
                <span className="cmp-bar" style={{ width: `${basePct}%` }} data-run="a" />
                <span className="mono cmp-value">{fmt(m.value_a)}</span>
              </div>
              <div className="cmp-bar-row" title={`本次 ${fmt(m.value_b)}`}>
                <span className="cmp-bar-label">本次</span>
                <span className="cmp-bar" style={{ width: `${nextPct}%` }} data-run="b" />
                <span className="mono cmp-value">{fmt(m.value_b)}</span>
              </div>
            </div>
          );
        })}
      </div>

      <div className="cmp-diff-grid">
        <div className="cmp-diff" role="region" aria-labelledby="cmp-regressions">
          <h4 id="cmp-regressions">
            新增失败样本 <span className="mono">{data.regression_count}</span>
          </h4>
          {data.regressions.length === 0 ? (
            <p className="muted">没有新增失败样本。</p>
          ) : (
            <ul className="cmp-list">
              {data.regressions.map((s) => (
                <li key={`r-${s.idx}`}>
                  <span className="mono">#{s.idx}</span> {s.question}
                  <div className="muted" style={{ fontSize: 12 }}>{s.reasons.join("；")}</div>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="cmp-diff" role="region" aria-labelledby="cmp-fixed">
          <h4 id="cmp-fixed">
            本次修复 <span className="mono">{data.fixed_count}</span>
          </h4>
          {data.fixed.length === 0 ? (
            <p className="muted">本次没有修复任何失败样本。</p>
          ) : (
            <ul className="cmp-list">
              {data.fixed.map((s) => (
                <li key={`f-${s.idx}`}>
                  <span className="mono">#{s.idx}</span> {s.question}
                  <div className="muted" style={{ fontSize: 12 }}>原问题：{s.reasons.join("；")}</div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  );
}