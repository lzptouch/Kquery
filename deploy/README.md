# deploy

部署资产集中在这里。三种部署方式的完整说明见 [`wiki/04-部署与运维.md`](../wiki/04-部署与运维.md)。

## 目录

| 路径 | 用途 |
| --- | --- |
| `docker/docker-compose.yml` | 全栈编排：etcd / milvus / redis / backend / init / frontend |
| `docker/backend.Dockerfile` | 后端镜像（多阶段，uv 装依赖 + 非 root 运行） |
| `docker/frontend.Dockerfile` | 前端镜像（vite 构建 + nginx 托管） |
| `docker/frontend.nginx.conf` | 前端容器内的 nginx 配置（SPA 回退 + 前缀反代） |
| `nginx/nginx.conf` | 可选统一入口反代（前后端同源） |
| `k8s/` | base / overlays / canary，Kustomize 组织 |
| `milvus/` | 只起 Milvus + etcd 的编排与运行参数 |
| `prometheus-rules.yaml` | 告警规则 |

## 镜像构建

构建上下文是**仓库根**（不是 `backend/` 或 `frontend/`），Dockerfile 内的 COPY 路径带模块前缀：

```bash
docker build -f deploy/docker/backend.Dockerfile  -t kquery/backend:local  .
docker build -f deploy/docker/frontend.Dockerfile -t kquery/frontend:local .
```

## 起全栈

```bash
cp backend/.env.example backend/.env      # 先填模型 Key
cd deploy/docker
docker compose up -d --build
```

端口：前端 8090、后端 8100、Milvus 19531、Redis 6381（刻意避开默认端口，改 `ports` 左侧即可）。

## 约定

- **部署文件只放这里**，不要再在 `backend/`、`frontend/` 或仓库根新增 Dockerfile / compose。
- 新增可配置路径时，同步更新 `tests/unit/test_nginx_prefix_parity.py`（前缀一致性守卫）与 `.dockerignore`。
- `milvus/volumes/`、`nginx/logs/` 是运行时目录，已在 `.gitignore` 中，不要提交。
