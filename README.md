# KQuery Pro — 企业级知识中台

[![Python 3.13](https://img.shields.io/badge/Python-3.13-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688)](https://fastapi.tiangolo.com/)
[![React 19](https://img.shields.io/badge/React-19-61DAFB)](https://react.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-5.7+-3178C6)](https://www.typescriptlang.org/)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind-3.4+-06B6D4)](https://tailwindcss.com/)
[![Milvus](https://img.shields.io/badge/Milvus-2.5+-00A4E4)](https://milvus.io/)
[![Neo4j](https://img.shields.io/badge/Neo4j-5.x-008CC1)](https://neo4j.com/)

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
│  ┌─────────────────┐  ┌─────────────────┐  ┌────────────────────┐   │
│  │ Gradio UI       │  │ React 前端      │  │ 直接 API 调用       │   │
│  │ (Python)        │  │ (Vite+TS)       │  │ (HTTP Client)      │   │
│  └─────────────────┘  └─────────────────┘  └────────────────────┘   │
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
│   │   ├── api/               # REST API 路由层
│   │   │   ├── main.py        # FastAPI 应用入口
│   │   │   ├── router.py      # RAG 核心 API（文档处理/查询/集合管理）
│   │   │   ├── agent_router.py # Agent 智能问答 API
│   │   │   ├── auth.py        # JWT 认证 API（注册/登录/刷新/登出）
│   │   │   ├── knowledge_graph.py # 知识图谱 RESTful API
│   │   │   ├── graphrag.py    # GraphRAG 混合检索 API
│   │   │   ├── health.py      # 健康检查（4 端点）
│   │   │   └── rbac.py        # RBAC 权限管理 API
│   │   ├── core/              # 核心基础设施层
│   │   │   ├── exceptions.py  # 统一异常体系（12 异常类 + 错误码枚举）
│   │   │   ├── settings.py    # Pydantic Settings（100+ 配置项）
│   │   │   ├── logger.py      # JSON 结构化日志 + 敏感数据脱敏
│   │   │   ├── middleware.py  # 请求日志中间件
│   │   │   ├── security.py    # 输入验证 + 安全响应头
│   │   │   ├── rate_limiter.py # Redis 滑动窗口限流
│   │   │   ├── audit_logger.py # 安全审计日志（22 种事件）
│   │   │   ├── cache.py       # L1 内存 + L2 Redis 多级缓存
│   │   │   ├── rbac.py        # RBAC 权限控制装饰器
│   │   │   ├── connection_pool.py # 多连接池管理
│   │   │   └── db_optimizer.py # 查询优化 + N+1 检测
│   │   ├── models/            # 数据模型层
│   │   │   ├── document.py    # 文档处理模型
│   │   │   ├── query.py       # 查询请求/响应模型
│   │   │   └── user.py        # 用户/角色/权限模型
│   │   ├── services/          # 业务逻辑层
│   │   │   ├── rag_service.py      # RAG 核心服务
│   │   │   ├── retriever.py        # 检索引擎（向量 + RRF + 重排）
│   │   │   ├── embedding.py        # 文本嵌入服务
│   │   │   ├── chunking.py         # 文本切分
│   │   │   ├── hyde_generator.py   # HyDE 假设性文档生成
│   │   │   ├── bm25_index_manager.py # BM25 索引管理
│   │   │   ├── bm25_retriever.py   # BM25 检索器
│   │   │   ├── image_understanding.py # 图像理解（阿里云 VL）
│   │   │   ├── table_understanding.py # 表格理解服务
│   │   │   ├── auth_service.py     # JWT 认证服务
│   │   │   ├── graph/              # 知识图谱服务
│   │   │   │   ├── entity_extraction.py    # 实体抽取
│   │   │   │   ├── relation_extraction.py  # 关系抽取
│   │   │   │   ├── graph_builder.py        # 图谱构建
│   │   │   │   ├── graph_query.py          # 图谱查询
│   │   │   │   ├── community_engine.py     # 社区发现
│   │   │   │   └── llm_graph_agent.py      # LLM 图谱 Agent
│   │   │   ├── graphrag/           # GraphRAG 引擎
│   │   │   │   ├── graphrag_engine.py      # 混合检索引擎
│   │   │   │   ├── qa_service.py           # 智能问答
│   │   │   │   └── visualization_service.py # 可视化数据转换
│   │   │   └── file_loaders/       # 文件加载器插件体系
│   │   │       ├── base.py
│   │   │       ├── pdf_loader.py
│   │   │       ├── word_loader.py
│   │   │       ├── excel_loader.py
│   │   │       ├── powerpoint_loader.py
│   │   │       ├── markdown_loader.py
│   │   │       ├── html_loader.py
│   │   │       ├── text_loader.py
│   │   │       ├── json_loader.py
│   │   │       ├── xml_loader.py
│   │   │       └── image_loader.py
│   │   ├── repositories/      # 数据访问层
│   │   │   ├── database/
│   │   │   │   └── milvus_connector.py
│   │   │   └── graph/
│   │   │       ├── neo4j_connector.py
│   │   │       └── schema.py
│   │   ├── agents/            # Agent 系统
│   │   │   ├── qa_agent.py    # 问答 Agent
│   │   │   ├── rag_tool.py    # RAG 工具
│   │   │   └── memory.py      # Agent 记忆管理
│   │   ├── config.py          # 全局配置管理
│   │   └── celery_config.py   # Celery 异步任务配置
│   ├── main.py                # 主入口（启动 FastAPI 和 Gradio）
│   ├── start_ui.py            # 独立 Gradio UI 启动脚本
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
│   │   │   ├── Settings.tsx        # 系统设置
│   │   │   ├── Profile.tsx         # 个人资料
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

- Python 3.13+
- Node.js 20+
- Milvus 2.5+
- Redis 7+
- Neo4j 5.x（可选，知识图谱功能）

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
| POST | `/auth/logout` | 用户登出 |
| GET | `/auth/me` | 获取当前用户信息 |
| PUT | `/auth/me/password` | 修改密码 |

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
| `/knowledge-bases` | 知识库管理 | 创建、配置和管理知识库集合 |
| `/documents` | 文档管理 | 上传、查看和管理文档 |
| `/knowledge-graph` | 知识图谱 | 图谱可视化浏览和探索 |
| `/search` | 全局搜索 | 搜索结果页面 |
| `/settings` | 系统设置 | 系统配置和参数调整 |
| `/profile` | 个人资料 | 用户信息和偏好设置 |
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
| Python | 3.13+ | 编程语言 |
| FastAPI | 0.115+ | API 框架 |
| Uvicorn | - | ASGI 服务器 |
| Pydantic | 2.x | 数据验证 |
| SQLAlchemy | 2.x | ORM |
| pymilvus | 2.5.x | Milvus 客户端 |
| Redis | 7+ | 缓存 |
| Neo4j | 5.x | 图数据库 |
| Celery | - | 异步任务队列 |
| Prometheus | - | 监控 |
| uv | - | Python 包管理 |

### 前端

| 技术 | 版本 | 用途 |
|------|------|------|
| React | 19 | UI 框架 |
| TypeScript | 5.7+ | 类型系统 |
| Vite | 6+ | 构建工具 |
| Tailwind CSS | 3.4+ | CSS 框架 |
| shadcn/ui | - | UI 组件库 |
| Radix UI | - | 底层组件 |
| Zustand | - | 状态管理 |
| React Router | v6 | 路由 |
| React Query | v5 | 服务端状态 |
| Axios | - | HTTP 客户端 |
| Lucide React | - | 图标库 |

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
| 架构文档 | `doc/ARCHITECTURE.md` | 详细技术架构和模块说明 |
| 知识图谱架构 | `doc/KNOWLEDGE_GRAPH_ARCHITECTURE.md` | 知识图谱详细设计 |
| 代码规范 | `doc/CODE_STANDARDS.md` | 项目代码规范 |
| 后端 README | `backend/README.md` | 后端详细文档 |
| 前端 README | `frontend/README.md` | 前端详细文档 |
| API 变更摘要 | `CODE_CHANGES_SUMMARY.md` | 最近 API 变更记录 |

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
