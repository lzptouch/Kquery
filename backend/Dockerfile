# syntax=docker/dockerfile:1
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
COPY pyproject.toml uv.lock* ./
RUN uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install -r pyproject.toml


# ---------- 运行阶段 ----------
FROM python:3.12-slim AS runtime

# 非 root 运行：容器逃逸时拿到的是无特权用户（stage-10 §1）
# curl 是 HEALTHCHECK 的客户端——镜像里没有 curl 时探针会永远失败，
# 而容器本身是好的，这种"探针假阴性"排查起来最费时间，所以要在这里显式装。
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