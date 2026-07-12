# KQuery Pro Gunicorn 配置
# 用于生产环境多 Worker 部署

import multiprocessing
import os

# 绑定地址
bind = "127.0.0.1:8000"

# Worker 数量（RAG 服务为 I/O 密集型，适度增加）
workers = min(multiprocessing.cpu_count() * 2 + 1, 8)
worker_class = "uvicorn.workers.UvicornWorker"

# 超时配置（RAG 检索可能较长）
timeout = 120
graceful_timeout = 30
keepalive = 5

# 日志配置
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")

# 进程命名
proc_name = "kquery"

# 优雅重启
max_requests = 1000
max_requests_jitter = 100

# 预加载应用
preload_app = True

# 后台运行
daemon = False