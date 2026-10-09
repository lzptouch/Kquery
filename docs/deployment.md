# Kquery 部署与验证指南

一键起全栈（Milvus + 后端 + 前端），并跑完接口与浏览器两层验证。

## 快速开始

```bash
docker compose up -d --build     # 首次约 3-5 分钟（要拉 Milvus 镜像）
docker compose ps                # 四个服务都应为 healthy
```

访问地址：

| 服务 | 地址 | 说明 |
|------|------|------|
| 前端 | http://localhost:8081 | 经 nginx，同时提供 SPA 与 API 反代 |
| 后端 | http://localhost:8000 | 直连，调试用 |
| Milvus | localhost:19530 | gRPC |
| Milvus 健康 | http://localhost:9091/healthz | |

停掉 / 清理：

```bash
docker compose down               # 停止，保留数据
docker compose down -v            # 连数据卷一起删（会丢向量索引与审计日志）
```

## 服务编排

```
kquery-etcd  ──┐
               ├─→ kquery-milvus ─→ kquery-init（一次性，建 Milvus 集合）
                                └──→ kquery-backend ─→ kquery-frontend
```

依赖顺序靠 `healthcheck` gate：后端等 Milvus 真健康、后端等 init 建表完成。
`start_period: 90s` 是必须的 —— Milvus 冷启动要加载本地存储，30s 会一直判定 unhealthy。

## 验证三层

```bash
# 1. 后端单元/接口测试（298 个用例）
cd backend && .venv/bin/python -m pytest -q

# 2. 接口冒烟：直连后端 与 经 nginx 各跑一遍
python3 scripts/smoke_api.py --base http://localhost:8000    # 67 项
python3 scripts/smoke_api.py --base http://localhost:8081    # 67 项，顺带验代理

# 3. 浏览器端到端（真实点击与输入）
cd backend && .venv/bin/python ../scripts/e2e_browser.py --base http://localhost:8081
```

跑脚本 3 需要浏览器：

```bash
cd backend && uv pip install playwright && .venv/bin/playwright install chromium
```

加 `--headed` 可以看着浏览器跑。

## 配置

密钥放 `backend/.env`，**不进镜像**（`.dockerignore` 已排除，由 compose 的 `env_file` 注入）。
无 Key 时设 `MODEL_PROVIDER=mock`，功能仍可跑通。

容器内会覆盖两项，因为 `.env` 里的地址是面向本机的：

```yaml
KQUERY_MILVUS_URI: http://milvus:19530   # 容器内走服务名，不是 localhost
DATA_DIR: /data                            # 数据卷挂载点
```

## 排障

| 症状 | 原因与处理 |
|------|-----------|
| 页面全白、控制台报 JS/CSS 404 | 构建产物路径与后端端点冲突。产物必须在 `/static/`（见下） |
| 接口 502，日志 `no resolver defined` | nginx 用变量做 `proxy_pass` 解析不了 Docker 服务名，必须用 `upstream` |
| SPA 路由 404 | `if` + `rewrite break` 改不出 SPA，需用 `error_page` 内部跳转 |
| 健康端点返回 HTML | 运维端点不能参与 SPA 分流，必须单独 `location` 透传 |
| 前端卡在"加载中"，日志刷满 | `useEffect` 依赖了每次产生新引用的 state，导致无限轮询 |
| SSE 一次性出现而非逐字 | nginx 代理 SSE 必须 `proxy_buffering off` |

### 两个值得记住的配置

**产物目录用 `/static/` 而不是 `/assets/`。**
后端已有 `/assets/{name}` 提供图片等媒体资源。两者同名时 nginx 的正则 location
优先匹配，会把 JS/CSS 请求代理给后端，页面直接白屏且报 404——看不出是路径冲突。

**`location ^~ /assets/` 的 `^~` 不能省。**
它表示"匹配到我就停，不再检查正则 location"，否则静态资源仍会落进 API 正则里。

## 单独调试

```bash
docker compose logs -f backend        # 后端日志（含请求 trace_id）
docker compose logs -f milvus         # 向量库日志
docker exec -it kquery-backend sh     # 进容器排查
docker compose restart backend        # 改代码后重启（代码在镜像里，需先 build）
```

改了后端代码要重新构建：

```bash
docker compose up -d --build backend
```

只改前端同理。数据卷里的索引不会因为重建容器而丢失。