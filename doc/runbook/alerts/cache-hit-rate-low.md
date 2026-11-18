# 处置卡：`KqueryCacheHitRateLow`

> **症状优先**：先看「用户看到什么」，再看「哪个组件坏了」。
> 值班通用流程见 [`../README.md`](../README.md)，分级见 [`../severity.md`](../severity.md)。

## 告警表达式

```promql
sum(rate(rag_cache_total{result="hit"}[10m])) / sum(rate(rag_cache_total[10m])) < 0.3（for 15m）
```

规则位置：`deploy/prometheus-rules.yaml`（告警 `KqueryCacheHitRateLow`）。

## 症状

用户侧：回答变慢（同问题重复问也重新跑模型）；**成本侧的信号更明显**——模型调用次数上涨。

## 第一诊断命令

```bash
curl -s localhost:8000/metrics | grep -E 'rag_cache_total|kquery_errors_total.*4501'
```

## 处置动作

1) 看 `kquery_errors_total{code="4501"}`（CACHE_UNAVAILABLE）：非 0 说明缓存后端在降级（Redis 不可用 → 退到内存缓存）；
2) 缓存后端降级 → 修 Redis（连接串 / 网络 / 内存上限）；
3) 无降级但命中率低 → 看 TTL 是否过短、键的维度是否过细（键含 `user` / `index_version` 时命中率天然低）。

## 回滚动作

回退缓存相关配置（`CACHE_BACKEND` / TTL）到上一版并重启。

## 升级条件

P3：不叫醒人，但**要跟进成本**——命中率长期 <30% 意味着模型成本失控。
