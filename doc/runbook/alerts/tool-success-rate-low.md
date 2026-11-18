# 处置卡：`KqueryToolSuccessRateLow`

> **症状优先**：先看「用户看到什么」，再看「哪个组件坏了」。
> 值班通用流程见 [`../README.md`](../README.md)，分级见 [`../severity.md`](../severity.md)。

## 告警表达式

```promql
sum(rate(rag_agent_tool_failures_total[5m])) / sum(rate(rag_agent_tool_calls_total[5m])) > 0.1（for 5m）
```

规则位置：`deploy/prometheus-rules.yaml`（告警 `KqueryToolSuccessRateLow`）。

## 症状

用户侧：Agent 回答里出现「工具不可用 / 未找到结果」这类内容，或答案不完整（迭代提前熔断）。

## 第一诊断命令

```bash
curl -s localhost:8000/metrics | grep rag_agent_tool
```

## 处置动作

1) 按 `error_class` 维度看归因：`approval_required`（等人审批，正常）/ `tripped`（工具熔断）/ `timeout`（上游慢）/ `param_invalid`（模型给错参数）；
2) `param_invalid` 偏高 → 多半是模型问题（提示词或工具 schema），不是工具坏了；
3) `tripped` 偏高 → 看该工具的 `kquery_upstream_circuit_open`，或调高工具超时。

## 回滚动作

把工具超时 / 熔断阈值回滚到基线；必要时临时摘掉故障工具的注册。

## 升级条件

P2：失败率 > 50% 或答案大面积不完整 → 升级二线。
