# 注意：这里**故意不写** `# syntax=docker/dockerfile:1`。
# 那行会让构建器去 Docker Hub 拉取 docker/dockerfile 前端镜像——在只能走
# 镜像加速站（或完全离线）的环境里，build 会在"解析构建器镜像"这一步就
# 超时失败，而本 Dockerfile 用到的全是 BuildKit 内置前端原生支持的能力
# （多阶段、COPY --from、COPY --chown）。
# ---------- 构建阶段：装依赖并预编译字节码 ----------
# 单独一层的意义：依赖清单不变时可命中缓存，改业务代码不必重装依赖。
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# build-essential / curl 只在各自需要的阶段出现，运行阶段不保留（减面）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# uv 比 pip 快一个数量级，且能输出锁文件——stage-10 §7 要求依赖可复现，
# 锁文件由本地 `uv lock` 生成后随仓库提交
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /build

# 先只拷依赖清单：这一步命中缓存后，业务代码改动不会触发重装。
# 锁文件单独 COPY 是有意的——锁文件变了才算依赖真的变了。
# 注意：这里**不写通配符**。`COPY uv.lock*` 在锁文件缺失时会静默跳过，
# 然后 `uv sync --frozen` 会在没有锁的情况下失败（或者更糟：悄悄用上索引版本）。
COPY pyproject.toml uv.lock ./
# 用锁文件安装：`uv pip install -r pyproject.toml` **不读 uv.lock**，
# 装出来的版本会随索引漂移 —— 镜像不可复现（同一 tag 今天明天内容不同）。
# `uv sync --frozen` 严格按锁文件装，且 --frozen 不会偷偷更新锁。
# UV_PROJECT_ENVIRONMENT 必填：uv sync 默认装进项目目录下的 .venv，
# 不指定就会把依赖装到 /build/.venv，而运行时 PATH 指向 /opt/venv → 依赖"消失"。
RUN uv venv /opt/venv \
    && UV_PROJECT_ENVIRONMENT=/opt/venv VIRTUAL_ENV=/opt/venv \
       uv sync --frozen --no-dev --no-install-project


# ---------- 运行阶段 ----------
FROM python:3.12-slim AS runtime

# 非 root 运行：容器逃逸时拿到的是无特权用户（stage-10 §1）
# curl 是 HEALTHCHECK 的客户端——镜像里没有 curl 时探针会永远失败，
# 而容器本身是好的，这种"探针假阴性"排查起来最费时间，所以要在这里显式装。
# 当前范围只有**图文**（视频入库 M3 已从计划里去掉），所以**不装 ffmpeg**
# ——它会给镜像加约 80MB 而用不上。等真要做视频时再加回来（并在计划里登记）。
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 kquery \
    && useradd --system --uid 1001 --gid kquery --create-home --home-dir /home/kquery kquery

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    VECTOR_STORE=milvus

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=kquery:kquery app ./app
COPY --chown=kquery:kquery .env.example ./

# 数据目录挂载点：向量索引、图谱快照、审计日志、备份都落这里
RUN mkdir -p /data && chown kquery:kquery /data
VOLUME ["/data"]

EXPOSE 8000

USER kquery

# 容器级健康检查打 /healthz：只判断"进程活着 + 关键依赖可达"。
# 深度自检（Milvus 集合、模型可用性）放在 /readyz，由编排层决定是否派流量——
# 两者混在一起会导致 Milvus 抖动把容器直接杀掉重启，而它其实还能服务读请求。
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]