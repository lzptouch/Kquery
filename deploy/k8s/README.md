# Kquery 的 K8s 部署（批次 5）

> **验证范围**：本目录用 `kind` / `k3s` 做过全栈验证（含探针、HPA、SSE 流式、摘流）。
> 生产环境（真实 Ingress Controller、cert-manager、托管 Milvus/Redis）**未验证**——
> 清单里的 Ingress 注解按 nginx-ingress 写，其它 Controller 需要对等改写。

## 目录

```
deploy/k8s/
├── base/                 # 基础清单（kustomize base）
│   ├── backend-deployment.yaml   # 后端 Deployment + Service + init CronJob
│   ├── frontend-deployment.yaml  # 前端 Deployment + Service
│   ├── ingress.yaml              # 含 SSE 必需的注解
│   ├── hpa.yaml                  # HPA + PDB + ServiceMonitor + NetworkPolicy
│   ├── configmap.yaml            # ConfigMap + PVC + Secret 模板
│   └── kustomization.yaml
├── canary/               # 部署层 canary（`OP-03`）
│   ├── backend-canary-deployment.yaml  # canary Deployment + 专属 Service（track=canary）
│   ├── ingress-canary.yaml            # 注解式流量切分（权重粒度粗，见文件头）
│   └── kustomization.yaml
└── overlays/{dev,prod}/  # 环境差异（副本数、资源、域名）
```

## 本地验证

```bash
# 1) 建集群
kind create cluster --name kquery

# 2) 渲染（不部署，先看渲染结果对不对）
kubectl kustomize deploy/k8s/overlays/dev

# 3) 部署
kubectl apply -k deploy/k8s/overlays/dev

# 4) 看探针与摘流
kubectl -n kquery get pods -w
kubectl -n kquery describe pod -l app=kquery,component=backend | grep -A3 Readiness

# 5) 验证 SSE 没被缓冲（应当**逐条**出现，而不是最后一次性出现）
curl -N -H 'Content-Type: application/json' \
  -d '{"question":"测试问题"}' http://kquery.local/api/ask/stream
```

## 关键决策（每条都有代价，别随手改）

| 决策 | 理由 | 代价 |
|---|---|---|
| `liveness` 用 `/livez`，**不用** `/healthz` | `/healthz` 查 9 个依赖，依赖抖动会**批量重启 Pod**（雪崩） | 依赖真挂了不会被 liveness 发现 → 由 `/readyz` 摘流解决 |
| `readiness` 用 `/readyz` | 依赖不可达时摘流；且**索引为空 ≠ 未就绪**（否则新部署永远没 endpoints） | 需要 `/readyz` 覆盖共享状态可达性 |
| Ingress 关缓冲 + 读超时 300s | SSE 不关缓冲就变成"一次性返回"；60s 超时会掐断长回答 | 需要 nginx-ingress（其它 Controller 需改写注解） |
| `preStop: sleep 10` + `terminationGracePeriodSeconds: 70` | SSE 连接需要时间排空 | 滚动更新慢 10s/实例 |
| `maxUnavailable: 0` | 滚动更新期间保持容量 | 需要足够余量 |
| HPA 上限 6 | **按上游模型配额反推**——扩到超过上游配额只是把等待搬到上游超时 | 突发流量可能被上游限流 |
| 副本数 = 2，`--workers` 保持 1 | 多副本需要状态外置（会话/检查点/图谱/检索配置）；未外置前**不要**开多副本 | 单副本容量上限 |
| Milvus 不进本清单 | 有状态服务的生命周期与无状态应用不同；生产用托管或官方 helm | 本地验证需另装 Milvus |
| Secret 只有占位符 | 真实值走 ExternalSecrets / SealedSecrets | 部署前必须替换 |

## 部署层 canary（`OP-03`）

```bash
# 1) 干跑渲染（先看渲染结果对不对）
kubectl kustomize deploy/k8s/canary

# 2) 部署 canary（与 base 同镜像同配置，只多一个 track=canary 标签）
kubectl apply -k deploy/k8s/canary

# 3) 判定（默认 dry-run：只输出判定与依据，不动集群）
backend/.venv/bin/python scripts/canary_promote.py \
  --prometheus-url http://prometheus:9090 \
  --rules '[{"metric":"error_rate","direction":"gt","threshold":0.05,"min_samples":30}]'

# 4) 达标才提升（显式 --apply）；不达标一律回滚
backend/.venv/bin/python scripts/canary_promote.py --apply --action promote ...
```

### 口径与代价（**写在这里，不藏起来**）

| 项 | 本实现 | 代价 |
|---|---|---|
| 流量权重 | **注解式**（`nginx.ingress.kubernetes.io/upstream-vhost`） | 权重**粒度粗**：≈「canary 副本数 / 总副本数」，不是精确百分比。要精确权重需引入服务网格（本项目未引入） |
| 判定口径 | 复用 `OP-02` 的 `services/canary.py`（同一套规则与控制器） | 多副本**必须**配 `prometheus_url`；未配置 → **无数据 → 不判定**（不猜数） |
| 回滚耗时 | 删掉 canary 资源 / 摘掉注解（秒级） | 回滚**快**是这套方案的主要优点 |
| 观察窗 | `--observe-minutes` | 未满观察窗不判定——启动瞬间的数字没有意义 |

> **canary 与 base 的标签必须不同**（`track: canary`）：标签相同会被 base 的 Service 选中，
> canary 与稳定版**共享流量**，canary 就失去了意义。

## 备份与恢复（生产必需）

| 数据 | 方式 | 频率 |
|---|---|---|
| Milvus 集合 | `milvus-backup` 快照到对象存储 | 每日 |
| 图谱 / 检查点 SQLite | PVC 快照（**先 `wal_checkpoint(TRUNCATE)`**，否则可能丢最后事务） | 每日 |
| 会话 / 检索配置（Redis） | AOF + 每日 RDB 归档 | 每日 |

恢复顺序：Milvus → Redis → backend（init Job 幂等，重复执行安全）。

## 已知未做

- **多副本的完整语义**：状态外置（批次 4）未完成前，副本数 > 1 会出现会话/检查点/图谱/检索配置分裂；
- **Prometheus adapter**：HPA 用自定义指标 `kquery_inflight_requests`，需装 prometheus-adapter 才能用；
- **真实 Ingress/TLS**：cert-manager、真实域名、NetworkPolicy 的实际效果未在生产验证。
