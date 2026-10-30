# Kquery 智能 RAG

生产级智能 RAG 系统（混合检索 + Agentic 编排 + GraphRAG + 多模态 + 企业护栏）。**系统现状分析见 `doc/analysis/`（15 篇），设计与演进计划见 `doc/plan/`。**

## 目录

| 目录 | 说明 | 技术栈 |
| --- | --- | --- |
| `backend/` | RAG 后端服务 | FastAPI + FAISS + 千问平台（uv 管理） |
| `frontend/` | Web 前端 | Vite + React + TypeScript（nvm node 24） |
| `doc/analysis/` | 系统现状分析（15 篇，按能力域编排） | — |
| `doc/plan/` | 设计与演进计划 | — |

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
cd backend && uv sync --extra dev                    # 必须带 --extra dev：playwright 在 extra 里，不在 dev 组
cd backend && uv run pytest                          # 全部测试（Mock 模型，无需 API Key）
cd backend && uv run pytest --cov=app                # 带覆盖率（门槛 80%，实测 85%）
cd backend && uv run ruff check app tests            # 静态检查
```

> `uv sync` **默认不装 `dev` extra**（playwright / pytest-playwright 在 extra 里），
> 而 `backend/tests/e2e_ui/` 的用例要 import playwright。
> 少装这一步时 `uv run pytest` 会在**收集阶段**就报
> `ModuleNotFoundError: No module named 'playwright'`，整片红——
> 看着像测试坏了，其实是依赖没装全。

当前 **1395 个用例**（后端，含 32 条 UI 用例需前端在跑 + 407 条格式专项）+ **54 个前端用例**（vitest）+ **53 项浏览器端到端断言**（`scripts/e2e_full.py`），覆盖率 85%。CI 见 `.github/workflows/ci.yml`（Linux 上跑真实 Milvus Lite，Windows 自动回退 FAISS）。

格式专项（**52 个支持扩展名**逐一过通用解析契约，详见 [`doc/testing/`](./doc/testing/README.md)）：

```bash
cd backend && uv run pytest tests/format -v
```

浏览器 UI 用例默认打 `127.0.0.1:5173`（vite dev server）。
打 Docker 部署的前端用：

```bash
cd backend && KQUERY_UI_WEB=http://127.0.0.1:8090 uv run pytest tests/e2e_ui -v
```

## 阶段进度

- [x] 阶段 1：Naive RAG 基线（切块 / 向量检索 / 引用溯源 / SSE 流式 / 统一信封 / 错误码）
- [x] 阶段 2：混合检索（BM25 + 向量 + RRF + gte-rerank-v2）与工程化（语义缓存 / 令牌桶限流 / Prometheus 指标 / 监控大盘 / 检索配置）
- [x] 阶段 3：智能分块与多模态（Recursive/Semantic/ParentChild/RAPTOR 四策略 + PDF 表格 / qwen-vl-ocr / paraformer-v2 + 入库任务队列 + 解析预览）+ Milvus 向量库
- [x] 阶段 4：Query 预处理（qwen-turbo 意图识别 / Fusion 改写 / HyDE / Step-Back / 多跳拆解 / 数据源路由 + 闲聊短路 + 分析记录可回放）
- [x] 阶段 5：自反思与自校正（CRAG 三分支 + 证据评分 + 幻觉自检 + 受限重生成 + 显式拒答 + 置信度 + 拒答反馈工单 + 质量指标）
- [x] 阶段 6：Agentic RAG（LangGraph 状态机 Plan/Tool/Reflect/Replan/Answer + 工具注册表与幻觉兜底 + 三层熔断 + 多轮会话存档 + 执行轨迹可视化）
- [x] 阶段 7：GraphRAG 与多模态（qwen-plus 实体关系抽取 + 消歧归一 + 内存图 BFS + 稳定编号社区摘要 + qwen-vl-max 结构化描述统一向量空间 + graph_search/image_search 工具 + 图谱浏览器）
- [x] 阶段 8：多智能体报告（Planner/Researcher/Validator/Writer/Critic 五角色 + Blackboard 黑板 + 拓扑分层并行 + Writer↔Critic 评分循环 + 报告中心）—— **已于 2026-10-17 下线**（下线记录见 `doc/plan/实施记录-doc-qa能力整合.md` 与 `doc/analysis/14-差距与演进.md` §六）
- [x] 阶段 9：企业级护栏（认证 Principal 四要素 + 租户隔离 + 文档级 ACL 双过滤（默认拒绝/deny 优先）+ 三个管理页面）—— 其中**评测平台、敏感词、配额、审计日志均已下线**（突发保护仍由令牌桶限流承担）
- [x] 整合 doc-qa 独有能力（2026-10-25，详见 `doc/plan/整合doc-qa独有功能到Kquery-分析与实施计划.md`）：
  Markdown 富文本渲染（marked + `[#N]` 角标）/ 浏览器语音输入（火山 STS，密钥留后端）/
  扩展解析格式（.xlsx + .pptx + 纯文本 + 代码文件）/ 单文档「快问」通道（临时索引 + TTL + 范围隔离 + 会话维度）/
  引文增强（参考汇总卡片 / 引文搜索 / 导出 TXT）/
  OCR / 语音转写**一律云服务 API**（不提供本地或自部署模型路径，已用守卫测试钉死）/
  推荐追问与会话自动命名 / 共享存储 Redis 化（缓存 + 限流 + 快问临时态，Fail-Open 降级）
- [ ] 阶段 10（部署演进）：规划见 `doc/plan/`

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
- 企业级护栏：认证 Principal 四要素 + 租户隔离 + 文档级 ACL 双过滤（默认拒绝/deny 优先）+ 突发令牌桶限流 + 管理页面（审计/配额/敏感词已下线）
- doc-qa 能力整合：Markdown 富文本渲染 / 浏览器语音输入（云服务 API） / 扩展格式（.xlsx .pptx 纯文本 代码）/ 单文档「快问」通道 / 引文增强（参考卡片 / 引文搜索 / 导出 TXT）/ 共享存储 Redis 化
