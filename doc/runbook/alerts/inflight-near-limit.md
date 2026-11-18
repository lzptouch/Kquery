# 处置卡：`KqueryInflightNearLimit`

> **症状优先**：先看「用户看到什么」，再看「哪个组件坏了」。
> 值班通用流程见 [`../README.md`](../README.md)，分级见 [`../severity.md`](../severity.md)。

## 告警表达式

```promql
kquery_inflight_requests > 28（for 5m）
```

规则位置：`deploy/prometheus-rules.yaml`（告警 `KqueryInflightNearLimit`）。

## 症状

用户侧：新请求开始排队，问答响应变慢（还没到 503，但能感觉到「转圈」变久）。

## 第一诊断命令

```bash
curl -s localhost:8000/metrics | grep kquery_inflight_requests
```

## 处置动作

1) 看 `kquery_upstream_latency_p95_ms{upstream=...}`：某条上游变慢 → 先降它的并发（`upstream_*_permits`）；
2) 上游正常但流量涨 → 确认是否上游配额够，再考虑扩容 `PIPELINE_WORKERS`；
3) 扩容前先核对：`pipeline_workers` 与各上游 permits 之和的关系（许可和 ≤ 线程数，否则线程全在等许可）。

## 回滚动作

把 `PIPELINE_WORKERS` 调回上一版配置并重启（扩容本身不改语义，回滚只是恢复基线）。

## 升级条件

P1：15 分钟内排队深度继续上涨、或开始出现 503 → 升级二线。
