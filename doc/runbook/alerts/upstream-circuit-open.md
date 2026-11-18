# 处置卡：`KqueryUpstreamCircuitOpen`

> **症状优先**：先看「用户看到什么」，再看「哪个组件坏了」。
> 值班通用流程见 [`../README.md`](../README.md)，分级见 [`../severity.md`](../severity.md)。

## 告警表达式

```promql
kquery_upstream_circuit_open == 1（for 1m；按 upstream 标签）
```

规则位置：`deploy/prometheus-rules.yaml`（告警 `KqueryUpstreamCircuitOpen`）。

## 症状

用户侧：该上游相关的功能变差或失败（LLM 不可用时问答返回「生成服务暂时不可用」的拒答文案，检索不可用时返回 502）；**不是白屏**——失败语义已经按码区分过。

## 第一诊断命令

```bash
curl -s localhost:8000/metrics | grep -E 'kquery_upstream_circuit_open|kquery_upstream_latency_p95'
```

## 处置动作

1) 按 upstream 标签定位是哪条通道（llm / embedding / rerank / milvus / multimodal）；
2) 检查平台配额与网络（熔断是「连续失败到阈值」的自动保护，不是配置问题）；
3) 配额被打满 → 提配额或临时降低该通道的并发（`upstream_*_permits`）；
4) 已熔断时上层会走降级（跳过重排 / 跳过改写 / 生成降级模板），**不用手工干预**。

## 回滚动作

调回 `upstream_*_permits` 与 `upstream_breaker_*` 的基线配置并重启（熔断会自行半开恢复）。

## 升级条件

P1：两条以上通道同时熔断 → 升级二线（说明是共享依赖或整体网络问题）。
