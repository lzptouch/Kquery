# Kquery 智能 RAG

生产级智能 RAG 系统：文档上传 → 切块 → 向量索引 → 混合检索（BM25 + 向量 + RRF + 重排）→ 千问生成 → 答案 + 引用溯源，含 Agentic RAG、GraphRAG、多租户与企业护栏。

## 目录

| 目录 | 说明 | 技术栈 |
| --- | --- | --- |
| `backend/` | RAG 后端服务 | FastAPI + FAISS + 千问平台（uv 管理） |
| `frontend/` | Web 前端 | Vite + React + TypeScript（nvm node 24） |
| `doc/` | 分析与计划文档 | — |

## 快速开始

```bash
# 向量库（Milvus standalone，etcd + 本地存储）
cd deploy/milvus && docker compose up -d

# 后端（8000 端口）
cd backend && cp .env.example .env   # 填入 DASHSCOPE_API_KEY；留空自动用 Mock 模型
uv sync && uv run uvicorn app.main:app --port 8000

# 前端（5173 端口，代理到 8000）
cd frontend && nvm use && npm install && npm run dev
```

打开 http://localhost:5173 → 「知识库」上传 PDF/Markdown → 「对话」提问，答案中带 `[#N]` 引用，点击可跳转证据。

## 测试

```bash
cd backend && uv run pytest                          # 全部测试（Mock 模型，无需 API Key）
cd backend && uv run pytest --cov=app                # 带覆盖率（门槛 80%，实测 85%）
cd backend && uv run ruff check app tests            # 静态检查
```

当前 **750+ 个用例**，覆盖率 85%。CI 见 `.github/workflows/ci.yml`（Linux 上跑真实 Milvus Lite，Windows 自动回退 FAISS）。

## 并发与容量（单进程单体）

| 维度 | 数值 | 依据 |
|---|---:|---|
| 轻接口吞吐（healthz/列表） | ~500 QPS | `scripts/load_test.py` 实测 |
| 在飞问答（生成中） | 32（`PIPELINE_WORKERS`） | `PipelineExecutor` 有界执行器 |
| 排队请求 | 200，超出 503 + Retry-After | 背压（快速失败） |
| 空闲/心跳 SSE 连接 | ≥500 | async 生成器 + 15s 心跳，不占线程 |
| Agent 并发 | 16（`AGENT_RUN_PERMITS`） | 独立闸门，超出 429 |
| 上游隔离仓 | llm=12 / embedding=4 / rerank=2 / milvus=8 | 之和 ≤ 问答线程数 |
| 入库 | 解析 2 并行 / 索引 1 串行，队列 200 | 队列满 503 |
| 单租户问答稳态 | 60/min（令牌桶 + 滑动窗口） | 成本护栏，设计值 |

压测：`cd backend && uv run python ../scripts/load_test.py --api http://localhost:8100`。
**注意**：上游模型配额才是问答容量的真实上限——扩容前先按分析文档的容量模型核算。

## 功能总览

- RAG 基线：切块 / 向量检索 / 引用溯源 / SSE 流式 / 统一信封 / 错误码
- 混合检索：BM25 + 向量 + RRF + gte-rerank-v2；语义缓存 / 令牌桶限流 / Prometheus 指标 / 监控大盘 / 检索配置
- 智能分块与多模态：Recursive/Semantic/ParentChild/RAPTOR 四策略 + PDF 表格 / qwen-vl-ocr / paraformer-v2 + 入库任务队列 + 解析预览 + Milvus 向量库
- Query 预处理：意图识别 / Fusion 改写 / HyDE / Step-Back / 多跳拆解 / 数据源路由 + 闲聊短路 + 分析记录可回放
- 自反思与自校正：CRAG 三分支 + 证据评分 + 幻觉自检 + 受限重生成 + 显式拒答 + 置信度 + 拒答反馈工单 + 质量指标
- Agentic RAG：LangGraph 状态机 Plan/Tool/Reflect/Replan/Answer + 工具注册表与幻觉兜底 + 三层熔断 + 多轮会话存档 + 执行轨迹可视化
- GraphRAG 与多模态：实体关系抽取 + 消歧归一 + 内存图 BFS + 社区摘要 + qwen-vl-max 结构化描述 + graph_search/image_search 工具 + 图谱浏览器
- 企业级护栏：认证 Principal 四要素 + 租户隔离 + 文档级 ACL 双过滤（默认拒绝/deny 优先）+ 审计只存哈希 + 敏感词扫描与掩码 + 租户配额 + 管理页面
