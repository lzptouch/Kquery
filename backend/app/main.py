"""Kquery 智能 RAG 后端入口（`A-12` 拆分后的**薄装配**）。

## 这个文件为什么这么短

装配曾经全部堆在本文件里（1282 行），后果是：
改一条路由要读 1000 行、`create_app()` 里三处重复建共享状态、
「谁先建」只存在于行号顺序里。现在按装配域拆开：

| 位置 | 职责 |
|---|---|
| `app/bootstrap.py` | 应用外壳：异常处理器 / 中间件 / 路由注册 |
| `app/assembly/lifespan.py` | 装配顺序（唯一真相源）与关停 |
| `app/assembly/runtime.py` | 指标 / 共享状态 / 限流 / 缓存 / 执行器 |
| `app/assembly/models.py` | LLM / Embedding / Rerank / OCR 怎么装 |
| `app/assembly/storage.py` | 索引别名 / 向量库 / BM25 / 文档存储 / 混合检索 |
| `app/assembly/knowledge.py` | 知识服务 / 任务队列 / 快问 / 校验历史 |
| `app/assembly/pipeline.py` | 查询预处理 / CRAG / 自检 / 问答编排 |
| `app/assembly/agent.py` | 工具目录 / 图谱 / 检查点 / 会话 / 运行器 |
| `app/assembly/hooks.py` | 入库后置钩子等跨域回调 |
| `app/api/probes.py` | `/health` · `/livez` · `/readyz` · `/healthz` · `/metrics` |

## 为什么还留一批同名导出

`app.main` 是**对外契约**（`uvicorn app.main:app`、测试、部署脚本都从这里进），
拆模块不该逼调用方改 import。因此这里保留探针相关导出；
新代码请直接引用 `app.bootstrap` / `app.assembly.*`，不要经由本模块。

**`_record_error` 不在兼容导出之列**：它只在异常处理器内部使用，
留在本模块会得到一个「打补丁无效的副本」。要改它请直接改 `app.bootstrap`。
"""
from __future__ import annotations

from app.bootstrap import create_app
from app.core.probes import (
    PROBE_ERROR_CODES,
    PROBE_STATUS_HTTP,
    alias_version as _alias_version,
    elapsed as _elapsed,
    env_of as _env_of,
    probe_all as _probe_all,
)

__all__ = [
    "PROBE_ERROR_CODES",
    "PROBE_STATUS_HTTP",
    "_alias_version",
    "_elapsed",
    "_env_of",
    "_probe_all",
    "app",
    "create_app",
]

app = create_app()
