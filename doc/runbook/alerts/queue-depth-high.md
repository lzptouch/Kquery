# 处置卡：`KqueryQueueDepthHigh`

> **症状优先**：先看「用户看到什么」，再看「哪个组件坏了」。
> 值班通用流程见 [`../README.md`](../README.md)，分级见 [`../severity.md`](../severity.md)。

## 告警表达式

```promql
kquery_queue_depth > 150（for 3m；队列上限 200）
```

规则位置：`deploy/prometheus-rules.yaml`（告警 `KqueryQueueDepthHigh`）。

## 症状

用户侧：部分请求被拒绝（503 + Retry-After），页面提示「服务繁忙，请稍后重试」。

## 第一诊断命令

```bash
curl -s localhost:8000/metrics | grep -E 'kquery_queue_depth|kquery_shed_total'
```

## 处置动作

1) `kquery_shed_total{reason=...}` 看拒入原因：`overloaded` = 真满了；`low_priority` = 后台任务在让路（正常）；
2) `overloaded` 时先查上游延迟（见 `inflight-near-limit` 卡）；
3) 确认后台任务（入库 / 图谱构建）是否在批量跑——它们会挤占排队位，可临时停掉。

## 回滚动作

停掉批量后台任务（入库队列 / 图谱构建），或把 `PIPELINE_QUEUE_SIZE` 调回基线。

## 升级条件

P1：已经开始大面积 503 → 立即升级二线。
