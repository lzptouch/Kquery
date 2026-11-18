# 处置卡：`KqueryLatencyHigh`

> **症状优先**：先看「用户看到什么」，再看「哪个组件坏了」。
> 值班通用流程见 [`../README.md`](../README.md)，分级见 [`../severity.md`](../severity.md)。

## 告警表达式

```promql
histogram_quantile(0.95, sum(rate(kquery_http_latency_seconds_bucket[5m])) by (le, route)) > 30（for 10m）
```

规则位置：`deploy/prometheus-rules.yaml`（告警 `KqueryLatencyHigh`）。

## 症状

用户侧：回答要等很久才出现（同步路径超过 30s；SSE 路径本就更长，注意区分）。

## 第一诊断命令

```bash
curl -s localhost:8000/metrics | grep kquery_http_latency_seconds_bucket
```

## 处置动作

1) 看 `kquery_stage_*`（阶段耗时）：慢在检索 / 重排 / 生成？
2) 慢在上游 → 看 `kquery_upstream_latency_p95_ms`；
3) 慢在生成 → 看是否触发了降级（`kquery_degrade_level` > 0 表示已进入降级档）；
4) 偶发长尾 → 看是否命中「生成降级模板」或重试（`rag_llm_calls_total` 的重试次数）。

## 回滚动作

回滚上游参数（超时 / 重试次数）到上一版；必要时临时收紧 `ASK_DEADLINE_MS`。

## 升级条件

P1：p95 > 60s（接近 `ask_deadline_ms` 的一半以上）→ 升级二线。
