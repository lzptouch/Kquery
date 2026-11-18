# 处置卡：`KqueryAgentIterationLimit`

> **症状优先**：先看「用户看到什么」，再看「哪个组件坏了」。
> 值班通用流程见 [`../README.md`](../README.md)，分级见 [`../severity.md`](../severity.md)。

## 告警表达式

```promql
avg_over_time(rag_agent_iterations_bucket[15m]) > 3（for 10m）
```

规则位置：`deploy/prometheus-rules.yaml`（告警 `KqueryAgentIterationLimit`）。

## 症状

用户侧：Agent 答案变啰嗦、绕圈子，或最终被判「不完整」（incomplete）。

## 第一诊断命令

```bash
curl -s localhost:8000/metrics | grep rag_agent_iterations
```

## 处置动作

1) 同时看工具失败率：失败率高 + 迭代高 = 工具在反复重试（先查工具，不要调迭代上限）；
2) 工具正常但迭代高 → 检索没召回有用证据（看 `rag_retrieval_*` 的 top score 是否偏低）；
3) 短期缓解：调低 CRAG 轮次（`CRAG_MAX_ITERATIONS`）或提高反思阈值（更早拒答而不是空转）。

## 回滚动作

回滚 CRAG / 反思相关配置到上一版。

## 升级条件

P3：不叫醒人（迭代高本身不致命），但**与工具失败率同时升高**时按 P2 处理。
