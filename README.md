# Kquery 智能 RAG

基于 `doc/series` 十阶段文档递进实现的生产级智能 RAG 系统。当前完成**阶段 1：最小可用 RAG**（文档上传 → 切块 → 向量索引 → 检索 → 千问生成 → 答案 + 引用溯源）。

## 目录

| 目录 | 说明 | 技术栈 |
| --- | --- | --- |
| `backend/` | RAG 后端服务 | FastAPI + FAISS + 千问平台（uv 管理） |
| `frontend/` | Web 前端 | Vite + React + TypeScript（nvm node 24） |
| `doc/series/` | 十阶段设计文档 | — |

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

当前 **336 个用例**，覆盖率 85%。CI 见 `.github/workflows/ci.yml`（Linux 上跑真实 Milvus Lite，Windows 自动回退 FAISS）。

## 阶段进度

- [x] 阶段 1：Naive RAG 基线（切块 / 向量检索 / 引用溯源 / SSE 流式 / 统一信封 / 错误码）
- [x] 阶段 2：混合检索（BM25 + 向量 + RRF + gte-rerank-v2）与工程化（语义缓存 / 令牌桶限流 / Prometheus 指标 / 监控大盘 / 检索配置）
- [x] 阶段 3：智能分块与多模态（Recursive/Semantic/ParentChild/RAPTOR 四策略 + PDF 表格 / qwen-vl-ocr / paraformer-v2 + 入库任务队列 + 解析预览）+ Milvus 向量库
- [x] 阶段 4：Query 预处理（qwen-turbo 意图识别 / Fusion 改写 / HyDE / Step-Back / 多跳拆解 / 数据源路由 + 闲聊短路 + 分析记录可回放）
- [x] 阶段 5：自反思与自校正（CRAG 三分支 + 证据评分 + 幻觉自检 + 受限重生成 + 显式拒答 + 置信度 + 拒答反馈工单 + 质量指标）
- [x] 阶段 6：Agentic RAG（LangGraph 状态机 Plan/Tool/Reflect/Replan/Answer + 工具注册表与幻觉兜底 + 三层熔断 + 多轮会话存档 + 执行轨迹可视化）
- [x] 阶段 7：GraphRAG 与多模态（qwen-plus 实体关系抽取 + 消歧归一 + 内存图 BFS + 稳定编号社区摘要 + qwen-vl-max 结构化描述统一向量空间 + graph_search/image_search 工具 + 图谱浏览器）
- [x] 阶段 8：多智能体报告（Planner/Researcher/Validator/Writer/Critic 五角色 + Blackboard 黑板 + 拓扑分层并行 + Writer↔Critic 评分循环 + 报告中心）
- [x] 阶段 9：企业级护栏（认证 Principal 四要素 + 租户隔离 + 文档级 ACL 双过滤（默认拒绝/deny 优先）+ 审计三事件只存哈希 + 敏感词长词优先扫描与掩码 + 租户滑动窗口配额 + 评测集与四指标运行 + 六个管理页面）
- [ ] 阶段 10：见 `doc/series/stage-10-deployment/README.md`
