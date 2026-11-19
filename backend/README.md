# Kquery 后端

FastAPI 服务：文档入库 → 切块 → 向量化 → 混合检索 → 重排 → LLM 生成（带引用）。

本文件只讲**怎么跑**；能力现状、接口契约与差距台账在仓库根 `README.md` 与 `doc/`。

## 目录约定

```
backend/
├── app/
│   ├── api/          # 路由层：只做参数校验 + 调服务 + 包信封
│   ├── core/         # 横切能力：配置 / 错误码 / 日志 / 追踪 / 探针 / 限流
│   ├── domain/       # 领域对象与 Pydantic 模型（无 IO）
│   ├── services/     # 业务实现：检索、入库、生成、Agent、图谱、多模态…
│   ├── assembly/     # 组装层：把配置解析成具体 provider 实例
│   ├── bootstrap.py  # 启动期装配
│   └── main.py       # ASGI 入口（只做装配与挂载，不写业务）
├── tests/（已废弃）  # 测试统一在仓库根 tests/，本目录不再放测试
├── pyproject.toml    # 依赖与 uv 配置（pytest/ruff 配置在仓库根）
└── .env.example      # 环境变量模板；.env 不入库
```

## 技术栈

- FastAPI + uvicorn，依赖用 uv 管理（`backend/.venv`、`backend/uv.lock`）
- 向量库：Milvus 服务（`deploy/docker` 的 standalone 或自建集群），可选 FAISS 本地索引
- 模型全部走云端 HTTP 接口，**无本地模型**：
  - 生成：DeepSeek（`/chat/completions`）或千问平台
  - 向量化：MiniMax `embo-01`（1536 维）或千问 `text-embedding-*`
  - 重排：DeepSeek 对话模型 listwise 重排，或 `gte-rerank`
  - OCR：`qwen-vl-ocr`
- Embedding / LLM / Reranker 均为协议接口，内置 Mock 实现供无凭据环境测试

## 快速开始

```bash
cd backend
cp .env.example .env          # 填 provider key；留空自动回落 Mock
uv sync --extra dev
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

生产容器由 `deploy/docker/docker-compose.yml` 起在 **8100**（避开同机其它项目的 8000）。

## 测试

测试不在本目录，统一在**仓库根** `tests/`（配置在根 `pyproject.toml`，放在
`backend/` 会被 pytest 完全忽略）：

```bash
cd ..                                    # 回到仓库根
uv run --project backend pytest tests/unit -q
```

## 端点

完整清单见 `GET /openapi.json` 与 [`wiki/03-接口参考.md`](../wiki/03-接口参考.md)。常用的：

| 端点 | 说明 |
| --- | --- |
| `GET /health` `GET /healthz` `GET /readyz` | 健康与就绪探针 |
| `POST /ask` `POST /ask/stream` | 同步 / SSE 流式问答（`answer + evidence[] + trace_id`） |
| `/knowledge/*` | 上传、列表、重建、删除、任务进度 |
| `/quickask/*` | 快问会话 |
| `/graph/*` | 图谱查询与浏览 |
| `/metrics` | Prometheus 指标（需 `X-Metrics-Token`） |
