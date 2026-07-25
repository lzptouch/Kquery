# KQuery Pro — 企业级知识中台

[![Python 3.12+](https://img.shields.io/badge/Python-3.12+-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.135+-009688)](https://fastapi.tiangolo.com/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.9+-3178C6)](https://www.typescriptlang.org/)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind-3.4+-06B6D4)](https://tailwindcss.com/)
[![Milvus](https://img.shields.io/badge/Milvus-2.6+-00A4E4)](https://milvus.io/)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.15+-008CC1)](https://neo4j.com/)

KQuery Pro 是下一代企业级知识检索与问答系统，基于 **GraphRAG**（知识图谱增强的检索增强生成）架构，融合向量语义检索与知识图谱推理，提供高精度、可解释的智能问答体验。

## 核心特性

### 1. GraphRAG 智能检索
- **混合检索引擎**：向量语义检索（60%）+ 知识图谱上下文扩展（40%），智能融合排序
- **8 种问题类型自适应**：事实型、关系型、推理型、列举型、比较型、定义型、程序型、未知类型
- **HyDE 假设性文档嵌入**：基于 LLM 生成假设答案，提升语义匹配精度
- **实体抽取与图谱扩展**：自动识别查询中的关键实体，扩展图谱邻居节点作为上下文

### 2. 知识图谱（Knowledge Graph）
- **自动图谱构建**：从文本中自动抽取实体和关系，构建结构化知识图谱
- **Neo4j 图数据库**：支持复杂图查询、路径分析、社区发现
- **图算法**：PageRank 实体重要性排序、Louvain/Label Propagation 社区发现
- **图谱可视化**：提供 D3.js/ECharts 兼容的可视化数据格式
- **一致性检查**：自动检测孤立节点、孤儿节点、重复实体等问题

### 3. Agent 智能问答
- **多轮对话**：支持会话管理和上下文记忆
- **工具调用**：RAG 搜索、文档处理等工具自动调用
- **证据追溯**：答案附带来源文档和图谱实体引用

### 4. 多格式文档支持
支持 12 种文件格式的自动解析与向量化：

| 类型 | 格式 | 说明 |
|------|------|------|
| 文档 | PDF、Word（doc/docx） | 含 OCR 图片文字提取 |
| 表格 | Excel（xls/xlsx） | 结构化表格理解 |
| 演示 | PowerPoint（ppt/pptx） | 幻灯片内容提取 |
| 文本 | TXT、Markdown、HTML | 纯文本处理 |
| 数据 | JSON、XML | 结构化数据解析 |

### 5. 企业级基础设施
- **JWT 认证 + RBAC 权限**：完整的用户、角色、权限管理体系
- **多级缓存**：L1 内存 LRU + L2 Redis 缓存，支持 `@cached` 装饰器
- **速率限制**：Redis 滑动窗口，支持 IP/用户/API Key 多层限制
- **审计日志**：22 种事件类型，敏感操作自动告警
- **健康检查**：支持 Kubernetes liveness/readiness 探针
- **监控体系**：Prometheus + Grafana 指标采集与可视化

---

## 技术架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                           客户端层                                    │
│  ┌─────────────────────────┐  ┌────────────────────┐              │
│  │ React 前端               │  │ 直接 API 调用       │              │
│  │ (Vite + TypeScript)      │  │ (HTTP Client)      │              │
│  └─────────────────────────┘  └────────────────────┘              │
└──────────────────────────────────────────────────────────────────────┘
           │                   │                      │
           ▼                   ▼                      ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      API 网关层（FastAPI）                             │
│  ┌─────────────────────────┐  ┌────────────────────────────────┐    │
│  │ /api/v1/kquery/*        │  │ /api/v1/graphrag/*             │    │
│  │ 文本 RAG 接口            │  │ 知识图谱检索接口                │    │
│  └─────────────────────────┘  └────────────────────────────────┘    │
│  ┌─────────────────────────┐  ┌────────────────────────────────┐    │
│  │ /api/v1/agent/*         │  │ /api/v1/auth/*                 │    │
│  │ Agent 智能问答           │  │ 认证授权（JWT+RBAC）            │    │
│  └─────────────────────────┘  └────────────────────────────────┘    │
│  ┌─────────────────────────┐  ┌────────────────────────────────┐    │
│  │ /api/v1/graph/*         │  │ /health  /metrics              │    │
│  │ 知识图谱管理             │  │ 健康检查与监控指标              │    │
│  └─────────────────────────┘  └────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
           │                   │
           ▼                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                           业务逻辑层                                   │
│  ┌──────────────────┐   ┌──────────────────┐   ┌─────────────────┐   │
│  │ 文本 RAG 服务     │   │ 知识图谱服务      │   │ QA Agent        │   │
│  │ rag_service.py   │   │ graph_builder    │   │ qa_agent.py     │   │
│  └────────┬─────────┘   └────────┬─────────┘   └─────────────────┘   │
│           │                      │                                    │
│  ┌────────┴────────────────────────────────────────────────────────┐  │
│  │                    FileLoaders 插件体系                           │  │
│  │  PDF | Word | Excel | PPT | MD | HTML | TXT | JSON | XML        │  │
│  │  Image(+OCR)                                                    │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  ┌──────────────────┐   ┌──────────────────┐   ┌─────────────────┐   │
│  │ 图像理解服务       │   │ 检索引擎          │   │ 表格理解服务     │   │
│  │ (阿里云VL)        │   │ (向量+RRF+重排)   │   │                 │   │
│  └──────────────────┘   └──────────────────┘   └─────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
           │                   │
           ▼                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                             数据层                                     │
│  ┌──────────────────┐  ┌────────────────┐  ┌──────────────────────┐  │
│  │ Milvus 向量数据库  │  │ Redis 缓存      │  │ Neo4j 图数据库        │  │
│  │ (文本向量)         │  │ (结果缓存)      │  │ (知识图谱)            │  │
│  └──────────────────┘  └────────────────┘  └──────────────────────┘  │
│  ┌──────────────────┐                                                │
│  │ 文件存储           │                                                │
│  │ ./documents/      │                                                │
│  └──────────────────┘                                                │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 项目结构

```
Kquery/
├── backend/                    # Python 后端服务
│   ├── app/
│   │   ├── api/               # REST API 路由层（按业务域拆分）
│   │   │   ├── main.py        # FastAPI 应用入口
│   │   │   ├── router.py      # 文本 RAG 核心 API（/api/v1/kquery）
│   │   │   ├── agent_router.py # Agent 智能问答 API（/api/v1/agent）
│   │   │   ├── auth_router.py # 认证 API（/api/v1/auth）
│   │   │   ├── rbac.py        # RBAC 权限管理 API（/api/v1/rbac）
│   │   │   ├── knowledge_graph.py # 知识图谱 RESTful API（/api/v1/graph）
│   │   │   ├── graphrag.py    # GraphRAG 混合检索 API（/api/v1/graphrag）
│   │   │   ├── knowledge_base_router.py # 共享知识库 API（/api/v1/knowledge-bases）
│   │   │   ├── notes_router.py # AI 笔记/写作 API（/api/v1/notes）
│   │   │   ├── mindmap_router.py # 思维导图 API（/api/v1/mindmap）
│   │   │   ├── tasks_router.py # 任务式 Agent API（/api/v1/tasks）
│   │   │   ├── skills_router.py # 技能生态 API（/api/v1/skills）
│   │   │   ├── llm_router.py  # LLM 模型切换/对话 API（/api/v1/llm）
│   │   │   ├── multimodal.py  # 多模态检索 API（/multimodal）
│   │   │   ├── chart_data.py  # 图表数据生成 API（/api/v1/chart-data）
│   │   │   ├── config_router.py # 运行时配置 API（/api/v1/config）
│   │   │   ├── ops_router.py  # 可观测性/运维 API（/api/v1/ops）
│   │   │   ├── memory_router.py # 长期记忆 API（/api/v1/memory）
│   │   │   ├── web_search_router.py # 联网搜索 API（/api/v1/web-search）
│   │   │   ├── speech_router.py # 语音识别/合成 API（/api/v1/speech）
│   │   │   ├── health.py      # 健康检查（/health）
│   │   │   ├── ws_manager.py  # WebSocket 会话管理
│   │   │   └── exception_handlers.py # 全局异常处理
│   │   ├── core/              # 核心基础设施层
│   │   │   ├── exceptions.py  # 统一异常体系
│   │   │   ├── settings.py    # Pydantic Settings 配置项
│   │   │   ├── logger.py      # JSON 结构化日志 + 敏感数据脱敏
│   │   │   ├── middleware.py  # 请求日志/审计中间件
│   │   │   ├── security.py    # 输入验证 + 安全响应头
│   │   │   ├── rate_limiter.py # Redis 滑动窗口限流
│   │   │   ├── audit_logger.py # 安全审计日志
│   │   │   ├── audit_storage.py # 审计日志存储
│   │   │   ├── cache.py       # L1 内存 + L2 Redis 多级缓存
│   │   │   ├── cache_warmer.py # 缓存预热
│   │   │   ├── rbac.py        # RBAC 权限控制
│   │   │   ├── auth.py        # JWT 认证依赖
│   │   │   ├── connection_pool.py # 多连接池管理
│   │   │   ├── db_optimizer.py # 查询优化 + N+1 检测
│   │   │   ├── database.py    # 数据库会话/引擎
│   │   │   ├── i18n.py        # 国际化
│   │   │   ├── kb_isolation.py # 知识库隔离
│   │   │   ├── tenant.py      # 租户隔离
│   │   │   ├── tracing.py     # 链路追踪
│   │   │   └── llm_client.py  # LLM 客户端封装
│   │   ├── models/            # 数据模型层（Pydantic）
│   │   │   ├── chat.py  common.py  document.py  graph.py
│   │   │   ├── graphrag.py  knowledge_base.py  lineage.py
│   │   │   ├── note.py  query.py  skill.py  user.py
│   │   ├── services/          # 业务逻辑层
│   │   │   ├── rag_service.py  retriever.py  embedding.py
│   │   │   ├── chunking.py  hyde_generator.py
│   │   │   ├── bm25_index_manager.py  bm25_retriever.py
│   │   │   ├── image_understanding.py  table_understanding.py
│   │   │   ├── document_classifier.py  document_models.py
│   │   │   ├── knowledge_base_service.py  storage_service.py
│   │   │   ├── lineage_service.py  long_term_memory_service.py
│   │   │   ├── skill_registry.py  task_service.py
│   │   │   ├── web_fetcher.py  web_search.py
│   │   │   ├── writing_prompts.py  writing_service.py
│   │   │   ├── graph/         # 知识图谱服务
│   │   │   │   ├── entity_extraction.py  relation_extraction.py
│   │   │   │   ├── graph_builder.py  graph_query.py
│   │   │   │   ├── community_engine.py  llm_graph_agent.py
│   │   │   │   ├── structured_extractor.py  table_mapper.py
│   │   │   ├── graphrag/       # GraphRAG 引擎
│   │   │   │   ├── graphrag_engine.py  qa_service.py
│   │   │   │   └── visualization_service.py
│   │   │   ├── file_loaders/   # 文件加载器插件体系
│   │   │   │   ├── base.py  pdf_loader.py  word_loader.py
│   │   │   │   ├── excel_loader.py  powerpoint_loader.py
│   │   │   │   ├── markdown_loader.py  html_loader.py  text_loader.py
│   │   │   │   ├── json_loader.py  xml_loader.py  image_loader.py
│   │   │   │   └── loader_config.py
│   │   │   ├── speech/         # 语音服务（ASR/TTS）
│   │   │   │   ├── asr_service.py  tts_service.py
│   │   │   ├── ops/            # 可观测性服务
│   │   │   │   ├── metrics_collector.py  log_collector.py
│   │   │   │   ├── health_checker.py  alert_manager.py
│   │   │   │   ├── incident_manager.py  slo_tracker.py
│   │   │   │   ├── ops_agent.py  _store.py
│   │   │   └── integration/    # 外部系统集成
│   │   │       ├── base.py  registry.py  crm_adapter.py
│   │   │       ├── order_sdk.py  wecom_integration.py
│   │   ├── repositories/      # 数据访问层
│   │   │   ├── base.py  connector.py  operations.py
│   │   │   └── graph/         # 图数据访问
│   │   │       ├── neo4j_connector.py  schema.py
│   │   ├── agents/            # Agent 系统
│   │   │   ├── qa_agent.py    # 问答 Agent
│   │   │   ├── rag_tool.py    # RAG 工具
│   │   │   ├── memory.py      # Agent 记忆管理
│   │   │   ├── orchestrator.py # 任务编排
│   │   │   ├── rule_engine.py # 规则引擎
│   │   │   └── decision_evaluator.py # 决策评估
│   │   ├── config.py          # 全局配置管理
│   │   └── celery_config.py   # Celery 异步任务配置
│   ├── main.py                # 主入口（启动 FastAPI）
│   ├── pyproject.toml         # uv 项目配置
│   ├── .env.example           # 环境变量示例
│   └── tests/                 # 测试目录
│
├── frontend/                  # React 前端应用
│   ├── src/
│   │   ├── App.tsx            # 主应用组件
│   │   ├── router.tsx         # React Router v6 路由配置
│   │   ├── main.tsx           # 入口文件
│   │   ├── lib/
│   │   │   ├── utils.ts       # 工具函数（cn/formatDate/formatFileSize）
│   │   │   └── api.ts         # API 客户端（axios 封装）
│   │   ├── components/
│   │   │   ├── layout/        # 布局组件
│   │   │   │   ├── Header.tsx
│   │   │   │   ├── Sidebar.tsx
│   │   │   │   └── MainLayout.tsx
│   │   │   └── ui/            # shadcn/ui 组件库
│   │   │       ├── alert.tsx, avatar.tsx, badge.tsx, button.tsx
│   │   │       ├── card.tsx, dialog.tsx, dropdown-menu.tsx
│   │   │       ├── input.tsx, progress.tsx, scroll-area.tsx
│   │   │       ├── select.tsx, table.tsx, tabs.tsx
│   │   │       ├── toast.tsx, tooltip.tsx
│   │   ├── pages/             # 页面组件
│   │   │   ├── Dashboard.tsx       # 首页概览
│   │   │   ├── Chat.tsx            # GraphRAG 智能问答
│   │   │   ├── KnowledgeBases.tsx  # 知识库管理
│   │   │   ├── Documents.tsx       # 文档管理
│   │   │   ├── KnowledgeGraph.tsx  # 知识图谱可视化
│   │   │   ├── SearchResults.tsx   # 全局搜索结果
│   │   │   ├── Analytics.tsx       # 分析与运维仪表盘
│   │   │   ├── MultimodalViewer.tsx # 多模态检索浏览
│   │   │   ├── Notes.tsx           # AI 笔记列表
│   │   │   ├── NoteEditor.tsx      # 笔记编辑器
│   │   │   ├── Skills.tsx          # 技能中心
│   │   │   ├── Tasks.tsx           # 任务式 Agent
│   │   │   ├── OpsCenter.tsx       # 运维中心
│   │   │   ├── Settings.tsx        # 系统设置
│   │   │   └── Login.tsx           # 登录页
│   │   ├── stores/            # Zustand 状态管理
│   │   │   ├── useAuthStore.ts     # 认证状态
│   │   │   ├── useChatStore.ts     # 聊天状态
│   │   │   ├── useDocumentStore.ts # 文档状态
│   │   │   ├── useGraphStore.ts    # 图谱状态
│   │   │   └── useKbStore.ts       # 知识库状态
│   │   └── assets/            # 静态资源
│   ├── index.html
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── tailwind.config.js
│   └── Dockerfile
│
├── deploy/                    # 部署配置
│   ├── 环境部署/               # 数据库、中间件、监控等基础设施
│   │   ├── docker-compose.yml     # 基础设施服务编排
│   │   ├── docker-compose.prod.yml # 生产环境基础设施
│   │   └── prometheus.yml         # Prometheus 监控配置
│   └── 项目部署/               # 前端、后端等应用服务
│       ├── docker-compose.yml     # 应用服务编排
│       ├── docker-compose.prod.yml # 生产环境应用服务
│       ├── backend/               # 后端部署文件
│       │   ├── Dockerfile              # API 服务镜像
│       │   └── gunicorn.conf.py        # Gunicorn 配置
│       └── frontend/              # 前端部署文件
│           └── Dockerfile              # 前端服务镜像
│
├── doc/                       # 项目文档
│   ├── ARCHITECTURE.md        # 架构文档
│   ├── CODE_STANDARDS.md      # 代码规范
│   ├── KNOWLEDGE_GRAPH_ARCHITECTURE.md # 知识图谱架构
│   └── ...
│
└── loadtest/                  # 压测脚本
    └── k6-sse-chat.js         # k6 压测脚本
```

---

## 快速开始

### 环境要求

- Python 3.12+
- Node.js 20+
- Milvus 2.5+
- Redis 7+
- Neo4j 5.15+（可选，知识图谱功能）

### 1. 克隆项目

```bash
git clone <repository-url>
cd Kquery
```

### 2. 启动依赖服务

```bash
cd deploy
docker-compose -f 环境部署/docker-compose.yml up -d milvus redis neo4j
```

### 3. 配置后端

```bash
cd backend
cp .env.example .env
# 编辑 .env 文件，配置 API 密钥和数据库连接
```

### 4. 启动后端

```bash
# 使用 uv（推荐）
uv sync
uv run python main.py

# 或使用 pip
pip install -r requirements.txt
python main.py
```

后端服务将启动在 `http://localhost:8000`，API 文档见 `http://localhost:8000/docs`。

### 5. 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端应用将启动在 `http://localhost:5173`。

---

## API 概览

### 认证授权（`/api/v1/auth`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/auth/register` | 用户注册 |
| POST | `/auth/login` | 用户登录（返回 JWT Token） |
| POST | `/auth/refresh` | 刷新 Token |
| GET | `/auth/me` | 获取当前用户信息 |

### RAG 核心（`/api/v1/kquery`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/kquery/documents/process` | 处理单个文档（上传 → 切分 → 向量化） |
| POST | `/kquery/documents/process-batch` | 批量处理文档 |
| POST | `/kquery/documents/query` | 向量语义查询 |
| POST | `/kquery/documents/add-text` | 直接添加文本到向量库 |
| GET | `/kquery/collections` | 列出所有向量集合 |
| GET | `/kquery/collections/{name}/stats` | 获取集合统计信息 |
| DELETE | `/kquery/collections/{name}` | 清空指定集合 |
| GET | `/kquery/supported-file-types` | 获取支持的文件类型 |
| GET | `/kquery/status` | 获取服务状态 |

### Agent 智能问答（`/api/v1/agent`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/agent/chat` | 与 Agent 对话 |
| POST | `/agent/upload` | 上传并处理文档 |
| GET | `/agent/session/{id}` | 获取会话信息 |
| DELETE | `/agent/session/{id}` | 清空会话历史 |
| GET | `/agent/collections` | 列出可用知识库集合 |
| GET | `/agent/status` | 获取 Agent 状态 |
| GET | `/agent/tools` | 列出可用工具 |

### 知识图谱（`/api/v1/graph`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/graph/entities` | 搜索或列出实体 |
| GET | `/graph/entities/{id}` | 获取实体详情 |
| POST | `/graph/entities/build` | 从文本构建图谱 |
| GET | `/graph/entities/{id}/neighbors` | 获取实体邻居 |
| GET | `/graph/path` | 查询实体间路径 |
| POST | `/graph/subgraph` | 提取局部子图 |
| GET | `/graph/algorithms/pagerank` | PageRank 排序 |
| GET | `/graph/algorithms/communities` | 社区发现 |
| GET | `/graph/statistics` | 图谱统计概览 |
| GET | `/graph/schema` | Schema 定义 |
| GET | `/graph/health` | Neo4j 健康检查 |
| POST | `/graph/consistency-check` | 一致性检查 |

### GraphRAG（`/api/v1/graphrag`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/graphrag/search` | GraphRAG 混合检索 |
| POST | `/graphrag/ask` | 智能问答（端到端） |
| GET | `/graphrag/question-type` | 问题分类测试 |
| POST | `/graphrag/entities` | 从查询抽取实体 |
| GET | `/graphrag/statistics` | 使用统计 |
| GET | `/graphrag/health` | 健康检查 |

### 共享知识库（`/api/v1/knowledge-bases`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/knowledge-bases` | 创建知识库 |
| GET | `/knowledge-bases` | 列出当前用户可见知识库 |
| GET | `/knowledge-bases/{kb_id}` | 获取知识库详情 |
| DELETE | `/knowledge-bases/{kb_id}` | 删除知识库 |
| GET | `/knowledge-bases/{kb_id}/members` | 列出成员 |
| POST | `/knowledge-bases/{kb_id}/members` | 添加成员 |
| DELETE | `/knowledge-bases/{kb_id}/members/{target_user_id}` | 移除成员 |
| PUT | `/knowledge-bases/{kb_id}/members/{member_id}/role` | 更新成员角色 |
| POST | `/knowledge-bases/{kb_id}/invites` | 生成邀请链接 |
| POST | `/knowledge-bases/join` | 通过邀请加入知识库 |

### AI 笔记与写作（`/api/v1/notes`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/notes` | 列出笔记 |
| POST | `/notes` | 创建笔记 |
| GET | `/notes/{id}` | 获取笔记详情 |
| PUT | `/notes/{id}` | 更新笔记 |
| DELETE | `/notes/{id}` | 删除笔记 |
| POST | `/notes/{id}/generate` | AI 生成/润色内容 |
| GET | `/notes/{id}/versions` | 查看历史版本 |
| POST | `/notes/versions/{version_id}/restore` | 恢复指定版本 |
| GET | `/notes/stats/overview` | 笔记统计概览 |
| GET | `/notes/tags/list` | 标签列表 |

### 思维导图（`/api/v1/mindmap`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/mindmap/generate` | 由文本/笔记生成思维导图 |
| GET | `/mindmap/templates/list` | 模板列表 |
| POST | `/mindmap/templates` | 创建模板 |
| DELETE | `/mindmap/templates/{id}` | 删除模板 |
| GET | `/mindmap/health` | 健康检查 |

### 技能生态（`/api/v1/skills`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/skills` | 技能列表 |
| GET | `/skills/categories` | 技能分类 |
| GET | `/skills/{skill_id}` | 技能详情 |
| POST | `/skills/{skill_id}/execute` | 执行技能 |
| GET | `/skills/health` | 健康检查 |

### 任务式 Agent（`/api/v1/tasks`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/tasks` | 任务列表 |
| POST | `/tasks` | 创建并执行任务 |
| GET | `/tasks/{task_id}` | 任务详情 |
| DELETE | `/tasks/{task_id}` | 删除任务 |
| POST | `/tasks/{task_id}/cancel` | 取消任务 |
| GET | `/tasks/{task_id}/log` | 任务日志 |
| POST | `/tasks/{task_id}/retry` | 重试任务 |
| GET | `/tasks/stats/overview` | 任务统计 |
| GET | `/tasks/health` | 健康检查 |

### LLM 模型（`/api/v1/llm`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/llm/models` | 可用模型列表 |
| GET | `/llm/status` | 模型服务状态 |
| POST | `/llm/chat/completions` | 模型对话补全 |
| GET | `/llm/health` | 健康检查 |

### 多模态检索（`/multimodal`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/multimodal/types` | 支持的多模态类型 |
| POST | `/multimodal/search` | 多模态内容检索 |
| GET | `/multimodal/content` | 多模态内容列表 |

### 图表数据（`/api/v1/chart-data`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/chart-data/generate` | 生成图表数据 |
| GET | `/chart-data/supported-types` | 支持的图表类型 |
| GET | `/chart-data/health` | 健康检查 |

### 运行时配置（`/api/v1/config`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/config/reload` | 重新加载配置 |
| GET | `/config/current` | 查看当前配置 |

### 可观测性/运维（`/api/v1/ops`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/ops/health/detailed` | 详细健康报告 |
| POST | `/ops/health/check` | 触发健康检查 |
| GET | `/ops/metrics/prometheus` | Prometheus 指标 |
| GET | `/ops/alerts` | 告警列表 |
| POST | `/ops/alerts/acknowledge` | 确认告警 |
| GET | `/ops/incidents` | 事件列表 |
| POST | `/ops/incidents` | 创建事件 |
| GET | `/ops/logs` | 日志查询 |
| GET | `/ops/dashboard` | 运维仪表盘数据 |
| GET | `/ops/slo` | SLO 列表 |
| POST | `/ops/slo` | 创建/更新 SLO |
| GET | `/ops/traces` | 链路追踪列表 |
| GET | `/ops/health` | 健康检查 |

### 长期记忆（`/api/v1/memory`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/memory` | 列出记忆事实 |
| DELETE | `/memory/{fact_id}` | 删除记忆 |
| DELETE | `/memory` | 清空记忆 |
| POST | `/memory/extract` | 从文本抽取记忆 |

### 联网搜索（`/api/v1/web-search`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/web-search` | 联网搜索 |
| POST | `/web-search/classify` | 查询分类 |
| GET | `/web-search/test` | 搜索服务连通性测试 |

### 语音（`/api/v1/speech`）

| 方法 | 端点 | 说明 |
|------|------|------|
| POST | `/speech/asr` | 语音识别（语音转文字） |
| POST | `/speech/tts` | 语音合成（文字转语音） |

### 健康检查（`/health`）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/health` | 基本健康检查 |
| GET | `/health/ready` | 就绪检查（含依赖服务状态） |
| GET | `/health/live` | 存活检查 |
| GET | `/health/detailed` | 详细健康报告 |

完整 API 文档可访问：`http://localhost:8000/docs`（Swagger UI）或 `http://localhost:8000/redoc`（ReDoc）。

---

## 前端页面

| 路由 | 页面 | 说明 |
|------|------|------|
| `/` | Dashboard | 首页概览，展示关键指标和快捷操作 |
| `/chat` | 智能问答 | GraphRAG 对话界面，支持知识库选择和多轮对话 |
| `/knowledge-bases` | 知识库管理 | 创建、配置和管理共享知识库集合 |
| `/documents` | 文档管理 | 上传、查看和管理文档 |
| `/knowledge-graph` | 知识图谱 | 图谱可视化浏览和探索 |
| `/search` | 全局搜索 | 搜索结果页面 |
| `/analytics` | 分析仪表盘 | 使用统计与运维指标可视化 |
| `/multimodal` | 多模态检索 | 图文多模态检索与浏览 |
| `/notes` `/notes/:id` | 笔记/写作 | AI 笔记协作与文档生成 |
| `/skills` | 技能中心 | 技能生态浏览与执行 |
| `/tasks` `/tasks/:id` | 任务 | 任务式 Agent 执行与监控 |
| `/ops` | 运维中心 | 可观测性、告警、SLO 与链路追踪 |
| `/settings` | 系统设置 | 系统配置和参数调整 |
| `/login` | 登录 | 用户登录页面 |

---

## 配置说明

核心配置文件为 `backend/.env`，主要配置项：

### 服务配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `API_HOST` | `0.0.0.0` | API 监听地址 |
| `API_PORT` | `8000` | API 端口 |
| `DEBUG` | `false` | 调试模式 |
| `ENVIRONMENT` | `development` | 运行环境 |

### 向量数据库

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MILVUS_HOST` | `localhost` | Milvus 地址 |
| `MILVUS_PORT` | `19530` | Milvus 端口 |
| `EMBEDDING_DIMENSION` | `1024` | 向量维度 |

### 嵌入模型

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EMBEDDING_MODEL_NAME` | `Qwen3-Embedding-8B` | 嵌入模型 |
| `EMBEDDING_API_BASE` | `http://localhost:7928/v1` | 嵌入 API 地址 |
| `EMBEDDING_API_KEY` | - | API 密钥 |

### Agent LLM

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `AGENT_MODEL_NAME` | `qwen-turbo` | Agent 模型 |
| `AGENT_API_BASE` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | API 地址 |
| `AGENT_API_KEY` | - | API 密钥 |

### 重排序模型

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RERANK_MODEL_NAME` | `Qwen3-Reranker-8B` | 重排序模型 |
| `RERANK_API_BASE` | `http://localhost:7929/v1` | API 地址 |

### 检索配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RAG_USE_HYDE` | `true` | 启用 HyDE 增强 |
| `RAG_USE_RRF` | `true` | 启用 RRF 融合 |
| `RAG_USE_RERANKER` | `true` | 启用重排序 |
| `RAG_INITIAL_TOP_K` | `15` | 初始召回数量 |
| `RAG_TOP_K` | `5` | 最终返回数量 |

完整配置列表见 `backend/.env.example`。

---

## 部署指南

### Docker 部署（推荐）

```bash
# 先启动基础设施（数据库、缓存等）
cd deploy
docker-compose -f 环境部署/docker-compose.yml up -d

# 再启动应用服务
docker-compose -f 项目部署/docker-compose.yml up -d
```

将启动以下服务：

| 服务 | 端口 | 部署分类 | 说明 |
|------|------|---------|------|
| etcd | 2379 | 环境部署 | Milvus 依赖 |
| minio | 9000/9001 | 环境部署 | 对象存储 |
| milvus | 19530/9091 | 环境部署 | 向量数据库 |
| redis | 6379 | 环境部署 | 缓存 |
| neo4j | 7474/7687 | 环境部署 | 图数据库 |
| mongodb | 27017 | 环境部署 | 文档存储 |
| elasticsearch | 9200 | 环境部署 | 日志存储 |
| logstash | 5044-5045 | 环境部署 | 日志采集 |
| kibana | 5601 | 环境部署 | 日志可视化 |
| prometheus | 9090 | 环境部署 | 监控采集 |
| grafana | 3000 | 环境部署 | 监控可视化 |
| kquery-api | 8000 | 项目部署 | 后端 API |
| kquery-frontend | 3000 | 项目部署 | 前端应用 |
| nginx | 80/443 | 项目部署 | 反向代理 |

### 生产环境 checklist

- [ ] 修改 `.env` 中的 `ENVIRONMENT=production`
- [ ] 配置强密码和 API 密钥
- [ ] 启用 HTTPS（配置反向代理如 Nginx/Caddy）
- [ ] 配置 Redis 密码
- [ ] 启用 API 密钥验证（`API_KEY_ENABLED=true`）
- [ ] 配置日志轮转和持久化
- [ ] 配置 Milvus 备份策略

---

## 技术栈

### 后端

| 技术 | 版本 | 用途 |
|------|------|------|
| Python | 3.12+ | 编程语言 |
| FastAPI | 0.135+ | API 框架 |
| Uvicorn | 0.42+ | ASGI 服务器 |
| Pydantic | 2.5+ | 数据验证 |
| SQLAlchemy | 2.x | ORM |
| pymilvus | 2.6+ | Milvus 客户端 |
| Redis | 5+ | 缓存 |
| Neo4j | 5.15+ | 图数据库 |
| Celery | 5.3+ | 异步任务队列 |
| LangChain | 1.2+ | Agent / LLM 编排 |
| DashScope | 1.14+ | 阿里云通义模型 SDK |
| MinIO | 7.2+ | 对象存储 |
| Prometheus | - | 监控 |
| uv | - | Python 包管理 |

### 前端

| 技术 | 版本 | 用途 |
|------|------|------|
| React | 19.2+ | UI 框架 |
| TypeScript | 5.9+ | 类型系统 |
| Vite | 8+ | 构建工具 |
| Tailwind CSS | 3.4+ | CSS 框架 |
| shadcn/ui | - | UI 组件库 |
| Radix UI | - | 底层组件 |
| Zustand | 5+ | 状态管理 |
| React Router | 6.28+ | 路由 |
| React Query | 5.60+ | 服务端状态 |
| Axios | 1.7+ | HTTP 客户端 |
| Lucide React | 0.460+ | 图标库 |
| Cytoscape / @antv/g6 / react-force-graph-2d | - | 图谱与关系可视化 |
| Recharts | 2.13+ | 图表 |
| React Markdown / remark-gfm | 9 / 4 | Markdown 渲染 |
| Zod / React Hook Form | 3 / 7 | 表单与校验 |

---

## 开发指南

### 后端开发

```bash
cd backend

# 安装依赖
uv sync

# 启动开发服务器（热重载）
uv run uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8000

# 运行测试
uv run pytest

# 代码格式化
uv run ruff format .
uv run ruff check . --fix

# 类型检查
uv run mypy app/
```

### 前端开发

```bash
cd frontend

# 安装依赖
npm install

# 启动开发服务器
npm run dev

# 构建生产版本
npm run build

# 代码检查
npm run lint
```

---

## 文档索引

| 文档 | 路径 | 说明 |
|------|------|------|
| 系统架构文档 | `doc/系统设计/系统架构文档.md` | 详细技术架构和模块说明 |
| 知识图谱架构设计 | `doc/系统设计/知识图谱架构设计.md` | 知识图谱详细设计 |
| 代码规范文档 | `doc/详细设计/代码规范文档.md` | 项目代码规范 |
| 技术分析报告 | `doc/详细设计/技术分析报告.md` | 技术选型与实现分析 |
| 模块执行流程与数据流 | `doc/详细设计/模块执行流程与数据流文档.md` | 模块调用与数据流说明 |
| 知识图谱开发指南 | `doc/详细设计/知识图谱开发指南.md` | 知识图谱开发实践 |
| 接口设计文档 | `doc/接口设计/接口设计文档.md` | 完整 API 接口设计 |
| 全功能测试计划 | `doc/测试计划/全功能测试计划.md` | 功能测试规划 |
| 功能与接口对照 | `doc/测试计划/功能与接口对照详细文档.md` | 功能点与接口映射 |
| UML 图表设计 | `doc/UML图示/UML图表设计.md` | UML 类图/时序图 |
| 后端 README | `backend/README.md` | 后端详细文档 |
| 前端 README | `frontend/README.md` | 前端详细文档 |
| API 文档 | `backend/API_DOCUMENTATION.md` | 后端 API 详细说明 |
| 更新日志 | `backend/CHANGELOG.md` | 版本变更记录 |

---

## 许可证

[MIT License](LICENSE)

---

## 贡献指南

欢迎提交 Issue 和 Pull Request！

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feature/my-feature`
3. 提交更改：`git commit -am 'Add some feature'`
4. 推送分支：`git push origin feature/my-feature`
5. 创建 Pull Request

---

**KQuery Pro Team** | 2026
