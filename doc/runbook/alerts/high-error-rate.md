# 处置卡：`KqueryHighErrorRate`

> **症状优先**：先看「用户看到什么」，再看「哪个组件坏了」。
> 值班通用流程见 [`../README.md`](../README.md)，分级见 [`../severity.md`](../severity.md)。

## 告警表达式

```promql
sum(rate(kquery_http_requests_total{status=~"5.."}[5m])) / sum(rate(kquery_http_requests_total[5m])) > 0.05（for 5m）
```

规则位置：`deploy/prometheus-rules.yaml`（告警 `KqueryHighErrorRate`）。

## 症状

用户侧：提问后立刻报错（5xx），页面显示「服务暂时不可用」；SSE 路径则是「error」事件。

## 第一诊断命令

```bash
curl -s localhost:8000/metrics | grep kquery_http_requests_total | grep 'status="5'
```

## 处置动作

1) 按 `route` 维度看是哪个端点在报错；
2) 看 `kquery_errors_total{code=...}`：码告诉你**是哪一类**（依赖不可用 / 检索失败 / 生成失败 / 内部错误）；
3) 按码查对应依赖（`/healthz` 给逐依赖状态）；
4) 错误码 5209（真相源损坏）→ **不要重启**，按 `backup.md` 走恢复流程。

## 回滚动作

回滚最近一次发布（配置或代码）；若与发布无关，按错误码定位依赖并切流量。

## 升级条件

P0：错误率 > 50% 或全部端点报错 → 立即叫人（决策者同步）。
