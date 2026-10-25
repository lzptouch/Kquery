/** 配额管理：总览（状态色板）+ 平台侧余量 + 单租户配置。 */
import { useCallback, useEffect, useState } from "react";
import { fetchPlatformQuota, fetchQuota, setQuota } from "../../../lib/api-client";
import type { PlatformQuota, QuotaRow } from "../../../types";

const STATUS_META = {
  ok: { label: "🟢 正常", className: "st-pass" },
  warning: { label: "🟠 临近上限", className: "st-warn" },
  exceeded: { label: "🔴 超额", className: "st-fail" },
} as const;

const PLATFORM_META = {
  disabled: { label: "⚪ 未配置预算（仅统计）", className: "" },
  ok: { label: "🟢 正常", className: "st-pass" },
  warning: { label: "🟠 余量不足 20%", className: "st-warn" },
  exhausted: { label: "🔴 已用尽（影响全部租户）", className: "st-fail" },
} as const;

export function QuotaPage() {
  const [rows, setRows] = useState<QuotaRow[]>([]);
  const [platform, setPlatform] = useState<PlatformQuota | null>(null);
  const [tenant, setTenant] = useState("default");
  const [perMinute, setPerMinute] = useState(60);
  const [perDay, setPerDay] = useState(10000);
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    const envelope = await fetchQuota();
    if (envelope.data) setRows(envelope.data);
    const p = await fetchPlatformQuota();
    if (p.data) setPlatform(p.data);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="page">
      <div className="page-inner">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>平台侧额度余量（账号级上游约束）</h3>
          {platform && (
            <>
              <div className="config-row">
                <span className={`status-tag ${PLATFORM_META[platform.status].className}`}>
                  {PLATFORM_META[platform.status].label}
                </span>
                <span className="mono">
                  今日已用 {platform.used_today}
                  {platform.budget_per_day > 0 ? ` / ${platform.budget_per_day}` : ""}
                  {platform.remaining !== null ? `（余 ${platform.remaining}）` : ""}
                </span>
                <span className="mono">
                  平台异常：限流 {platform.platform_errors.rate_limited} · 配额 {platform.platform_errors.quota} ·
                  超时 {platform.platform_errors.timeout}
                </span>
              </div>
              {platform.budget_per_day > 0 && (
                <div className="quota-bar" aria-hidden="true">
                  <div
                    className={`quota-bar-fill ${platform.status === "exhausted" ? "quota-fill-fail" : platform.status === "warning" ? "quota-fill-warn" : "quota-fill-pass"}`}
                    style={{ width: `${Math.min(platform.ratio * 100, 100)}%` }}
                  />
                </div>
              )}
              <div className="trace-id" style={{ marginTop: 6 }}>
                应用侧配额只影响单租户；平台侧限流影响<strong>全部租户</strong>，是唯一能提前预警“整个系统即将不可用”的指标。
              </div>
            </>
          )}
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>配额总览</h3>
          <table className="files-table">
            <thead>
              <tr><th>租户</th><th>分钟上限</th><th>当前窗口</th><th>今日调用</th><th>每日上限</th><th>状态</th></tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.tenant_id}>
                  <td className="mono">{r.tenant_id}</td>
                  <td className="mono">{r.limit_per_minute}/min</td>
                  <td className="mono">{r.current_window}</td>
                  <td className="mono">{r.today_count}</td>
                  <td className="mono">{r.limit_per_day}</td>
                  <td><span className={`status-tag ${STATUS_META[r.status].className}`}>{STATUS_META[r.status].label}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h3 style={{ marginTop: 0 }}>租户配额配置</h3>
          <div className="config-row">
            <label>租户 <input className="probe-input" style={{ marginLeft: 6 }} value={tenant} onChange={(e) => setTenant(e.target.value)} /></label>
            <label>每分钟 <input type="number" className="probe-input" style={{ width: 90, marginLeft: 6 }} value={perMinute} onChange={(e) => setPerMinute(Number(e.target.value))} /></label>
            <label>每日 <input type="number" className="probe-input" style={{ width: 110, marginLeft: 6 }} value={perDay} onChange={(e) => setPerDay(Number(e.target.value))} /></label>
            <button
              type="button" className="btn primary"
              onClick={() => {
                void setQuota(tenant, perMinute, perDay).then(() => {
                  setSaved(true);
                  setTimeout(() => setSaved(false), 2000);
                  void load();
                });
              }}
            >
              保存
            </button>
            {saved && <span className="status-tag st-pass">已保存</span>}
          </div>
          <div className="trace-id" style={{ marginTop: 6 }}>
            超限返回 429 + Retry-After（前端倒计时自动重试）；滑动窗口保证任意 60 秒内不超上限。
          </div>
        </div>
      </div>
    </div>
  );
}
