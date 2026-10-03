/** 检索参数配置（/admin/retrieval-config，阶段 2 前端规范 §4）。
 *
 * 保存不立即生效：后端返回生效时刻，页面显著提示"修改将于 HH:MM:SS 生效"。
 */
import { useCallback, useEffect, useState } from "react";
import { fetchRetrievalConfig, resetRetrievalConfig, saveRetrievalConfig } from "../../lib/api-client";
import type { RetrievalConfig } from "../../types";

const RERANK_MODELS = ["gte-rerank-v2"];

export function RetrievalConfigPage() {
  const [config, setConfig] = useState<RetrievalConfig | null>(null);
  const [pendingInfo, setPendingInfo] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const envelope = await fetchRetrievalConfig();
      if (envelope.data) setConfig(envelope.data.active);
    } catch {
      setError("配置加载失败");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const patch = (p: Partial<RetrievalConfig>) => setConfig((prev) => (prev ? { ...prev, ...p } : prev));

  const save = async (cfg: RetrievalConfig) => {
    setSaving(true);
    setError(null);
    try {
      const envelope = await saveRetrievalConfig(cfg);
      if (envelope.data) setPendingInfo(envelope.data.message);
    } catch {
      setError("保存失败，请重试");
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setSaving(true);
    try {
      const envelope = await resetRetrievalConfig();
      if (envelope.data) setPendingInfo(envelope.data.message);
      await load();
    } finally {
      setSaving(false);
    }
  };

  if (!config) {
    return (
      <div className="page">
        <div className="page-inner">
          <div className="skeleton" style={{ height: 200 }} />
        </div>
      </div>
    );
  }

  const weightSum = (config.vector_enabled ? config.vector_weight : 0) + (config.bm25_enabled ? config.bm25_weight : 0);
  const weightInvalid = weightSum <= 0;

  return (
    <div className="page">
      <div className="page-inner">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>检索参数配置</h3>
          {pendingInfo && (
            <div className="pending-note" role="status">
              ℹ {pendingInfo}（配置异步生效，无需重启服务）
            </div>
          )}
          {error && (
            <div className="error-inline" role="alert">
              <span aria-hidden>⚠</span>
              {error}
            </div>
          )}

          <h4>召回策略</h4>
          <div className="config-row">
            <label>
              <input
                type="checkbox"
                checked={config.vector_enabled}
                onChange={(e) => patch({ vector_enabled: e.target.checked })}
              />
              向量检索
            </label>
            <label className="trace-id">
              权重
              <input
                type="number"
                min={0}
                max={100}
                step={0.1}
                value={config.vector_weight}
                disabled={!config.vector_enabled}
                onChange={(e) => patch({ vector_weight: Number(e.target.value) })}
              />
            </label>
          </div>
          <div className="config-row">
            <label>
              <input
                type="checkbox"
                checked={config.bm25_enabled}
                onChange={(e) => patch({ bm25_enabled: e.target.checked })}
              />
              BM25 关键词
            </label>
            <label className="trace-id">
              权重
              <input
                type="number"
                min={0}
                max={100}
                step={0.1}
                value={config.bm25_weight}
                disabled={!config.bm25_enabled}
                onChange={(e) => patch({ bm25_weight: Number(e.target.value) })}
              />
            </label>
          </div>
          {weightInvalid && <div className="error-inline">至少启用一路召回且权重和大于 0</div>}

          <h4>重排</h4>
          <div className="config-row">
            <label>
              <input
                type="checkbox"
                checked={config.rerank_enabled}
                onChange={(e) => patch({ rerank_enabled: e.target.checked })}
              />
              千问重排
            </label>
            <select
              value={config.rerank_model}
              disabled={!config.rerank_enabled}
              onChange={(e) => patch({ rerank_model: e.target.value })}
              aria-label="重排模型"
            >
              {RERANK_MODELS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <label className="trace-id">
              Top-K
              <input
                type="number"
                min={1}
                max={10}
                value={config.top_k}
                onChange={(e) => patch({ top_k: Math.max(1, Math.min(10, Number(e.target.value))) })}
              />
            </label>
          </div>

          <h4>缓存</h4>
          <div className="config-row">
            <label className="trace-id">
              TTL（秒）
              <input
                type="number"
                min={60}
                value={config.cache_ttl_s}
                onChange={(e) => patch({ cache_ttl_s: Math.max(60, Number(e.target.value)) })}
              />
            </label>
            <span className="trace-id">模型版本已编入缓存键（升级自动失效，无需手动清缓存）</span>
          </div>

          <h4>限流（按租户）</h4>
          <div className="config-row">
            <label className="trace-id">
              速率（次/分钟）
              <input
                type="number"
                min={1}
                value={config.ratelimit_per_min}
                onChange={(e) => patch({ ratelimit_per_min: Math.max(1, Number(e.target.value)) })}
              />
            </label>
            <label className="trace-id">
              桶容量
              <input
                type="number"
                min={1}
                value={config.ratelimit_capacity}
                onChange={(e) => patch({ ratelimit_capacity: Math.max(1, Number(e.target.value)) })}
              />
            </label>
          </div>

          <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
            <button
              type="button"
              className="btn btn-primary"
              disabled={saving || weightInvalid}
              onClick={() => void save(config)}
            >
              {saving ? "保存中…" : "保存"}
            </button>
            <button type="button" className="btn" disabled={saving} onClick={() => void reset()}>
              重置为默认
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
