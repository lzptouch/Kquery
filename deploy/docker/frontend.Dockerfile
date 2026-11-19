# 注意：这里**故意不写** `# syntax=docker/dockerfile:1`。
# 那行会让构建器去 Docker Hub 拉取 docker/dockerfile 前端镜像——在只能走
# 镜像加速站（或完全离线）的环境里，build 会在"解析构建器镜像"这一步就
# 超时失败，而本 Dockerfile 用到的全是 BuildKit 内置前端原生支持的能力
# （多阶段、COPY --from、COPY --chown）。
# 构建上下文是**仓库根**（不是 frontend/）：
#   docker build -f deploy/docker/frontend.Dockerfile -t kquery/frontend .
# 所以下面的 COPY 路径都带 `frontend/` 前缀；nginx 配置从 deploy/docker/ 取。
#
# ---------- 构建阶段 ----------
FROM node:22-alpine AS builder

ENV CI=true

WORKDIR /app

# 先只拷依赖清单，命中缓存后再拷源码：
# 只改业务代码时不必重装依赖（node_modules 安装通常是构建里最慢的一步）
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund 2>/dev/null || npm install --no-audit --no-fund

COPY frontend/ ./

# 镜像里不能带任何后端地址：运行时后端主机名由 nginx 从环境变量读，
# 同一份镜像才能在本地/测试/生产复用（stage-10 §7 配置外置）
# 前端用相对路径调用后端（/ask、/knowledge…），由 nginx 按根路径前缀分流，
# 不需要 API base 环境变量。此前的 VITE_API_BASE_URL=/api 是残留：源码零引用，
# 且 /api 前缀与 nginx 的 location 清单对不上，留着只会误导。

RUN npm run build


# ---------- 运行阶段 ----------
FROM nginx:1.27-alpine AS runtime

# 非 root：nginx 官方镜像的 master 进程默认以 root 启动再降权给 worker，
# 这里让 master 也以非 root 跑（stage-10 §1）
RUN apk add --no-cache curl \
    && rm -rf /var/cache/apint/*

COPY --from=builder /app/dist /usr/share/nginx/html
COPY deploy/docker/frontend.nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8080/ || exit 1

CMD ["nginx", "-g", "daemon off;"]