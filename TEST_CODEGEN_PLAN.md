# Kquery 测试体系分析与代码生成计划

> **版本**: v1.2（2026-08-08 细化版）
> **上一版**: v1.1（代码复核修订版）。本文档仅含分析与计划，不改动任何代码。目标：根目录创建 `test/` 及 5 个测试子目录（单元/集成/API/UI/压力），规划各目录测试代码。
> v1.2 变更：在 v1.1 基础上细化——配置示例（§3.1.1）、单元用例明细（§3.2.1）、集成链路步骤（§3.3.1）、API fixture 与用例矩阵（§3.4.1/§3.4.2）、UI 场景步骤（§3.5.1）、压力场景参数（§3.6.1）、环境准备（§四）、里程碑验收（§五）。

---

## 修订记录（v1.1 → v1.2，本版细化；v1.0 → v1.1 见下）

| # | v1.0 问题 | 修正内容 | 严重度 |
|---|---|---|---|
| R1 | 称响应为统一 `code/message/data` 包裹 | **错误**。实际成功响应直接返回 `response_model`（无包裹）；错误统一 `{"error":{code,message,status,details},"request_id"}`。契约断言规则重写（§3.4） | 高 |
| R2 | 需 mock langchain/dashscope | 项目自带 `core/llm_client.py` + `core/mock_data/`（qa/embeddings/graph/hyde/image 内置 Mock），`is_llm_configured()` 未配置 API Key 时自动降级 Mock。**测试不配 Key 即走内置降级**，单测再 mock `llm_client` 层 | 高 |
| R3 | API 测试仅覆盖 6 个路由 | `main.py` 实际注册 **18 个 router**，补 llm/multimodal/web_search/speech/memory/tasks/skills/chart/config 等（§3.4） | 高 |
| R4 | 单测模块映射不完整 | 补 `llm_client`、`security`、`audit_logger/storage`、`cache_warmer`、`connection_pool`、`hyde_generator`、`image/table_understanding`、`web_search`、`long_term_memory`、`task_service`、`writing_service`、`graphrag/*`、`speech/*`、`workflow/engine`、`skills/builtin`、`agents/memory|rag_tool|decision_evaluator`（§3.2） | 中 |
| R5 | 称前端有独立 chat/document service | chat/文档/检索实际走 `src/lib/api.ts` 的 `agentApi/documentApi/queryApi/collectionApi`，需补该文件单测；8 个 services 为 auth/kb/chart/graph/multimodal/note/skill/task（§3.2） | 中 |
| R6 | 未提前端代理端口不一致 | `vite.config.ts` proxy `/api→:8080`，而 `lib/api.ts` 注释与既有文档用 **8000**。UI/E2E 前置须统一（§3.1 P0-4） | 中 |
| R7 | pytest 规划未衔接现有配置 | 存量 `pyproject.toml` 已有 `testpaths=["tests"]`、markers `unit/integration/slow`（无 `api`）。给出 A/B 两方案（§3.1 P0-2） | 中 |
| R8 | 未提 TestClient 触发 startup 副作用 | startup 会连 Redis/DB/Neo4j、初始化审计表。API/集成测试须保证依赖可用或隔离 startup（§3.4 前置） | 高 |
| R9 | UI spec 仅 6 页面 | 前端路由共 15 页面，补 skills/tasks/search/analytics/multimodal（§3.5） | 低 |
| R10 | 未利用根目录 yaml | 根目录 6 个 `*.yaml`（chat/kb/main/notes/skills/tasks）确认是 Playwright 页面结构快照（`generic [ref=e2]`），作 E2E 断言参考（§3.5） | 低 |

---

## 一、项目现状分析（代码复核版）

### 1.1 技术栈

| 端 | 技术栈 | 入口 |
|---|---|---|
| 后端 | FastAPI (Py≥3.12) + Uvicorn，v3.0.0，Prometheus `/metrics`（enable_metrics） | `backend/main.py` → `app.api.main:app` |
| 前端 | React 19 + TS + Vite 8 + Zustand 5 + TanStack Query + axios | `frontend/src/main.tsx`；`vite.config.ts`：port 5173，`/api→http://localhost:8080`，alias `@→src` |
| 基础设施 | Milvus、Neo4j(APOC)、Redis、MongoDB、MinIO、ELK、Celery | `deploy/环境部署/docker-compose.yml`；测试用 `docker-compose.test.yml` |

### 1.2 后端架构（代码确认）

- **路由**（`app/api/main.py`，18 个 router，除 health 外均挂 `/api/v1`）：health（无前缀 `/health`）、业务主路由 `router.py`（`/kquery/*` 文档/集合/检索 + `agent_router.py` `/agent/chat` + `ws_manager.py` WebSocket）、`knowledge_graph.py`、`graphrag.py`、`config_router.py`、`chart_data.py`、`multimodal.py`、`llm_router.py`、`notes_router.py`、`mindmap_router.py`、`web_search_router.py`、`speech_router.py`、`memory_router.py`、`tasks_router.py`、`auth_router.py`（register/login/refresh/me）、`knowledge_base_router.py`、`skills_router.py`、`ops_router.py`。
- **响应契约**：成功直接返回 `response_model`；失败统一 `{"error":{code,message,status,details},"request_id"}`（`exception_handlers.py`），429 带 `Retry-After`/`X-RateLimit-*`，Pydantic 校验失败为 `VAL_002` 结构。
- **LLM 统一入口** `core/llm_client.py`：`call_openai_chat`/`call_dashscope`/`is_llm_configured`/`get_mock_data`，tenacity 重试 + `core/mock_data/` 内置 Mock 降级。
- **模块**：`agents/`（qa_agent、orchestrator、memory、rag_tool、rule_engine、decision_evaluator）、`services/`（rag_service、retriever、bm25_*、chunking、embedding、hyde_generator、graph/、graphrag/、file_loaders/13 类、speech/、ops/、integration/、web_search、long_term_memory_service、task_service、writing_service、image/table_understanding、lineage_service、storage_service）、`core/`（auth、security、rbac、tenant、kb_isolation、cache L1+L2、cache_warmer、connection_pool、rate_limiter、audit_logger、audit_storage、database、exceptions、i18n、tracing、middleware、settings、llm_client、session/、llm/、mock_data/）、`repositories/`（database/milvus_connector、graph/neo4j_connector+schema）、`plugins/`（base/manager/registry）、`workflow/engine.py`、`skills/builtin/`、`celery_config.py`、`webUI/`。

### 1.3 前端架构（代码确认）

- **15 页面路由**（`src/router.tsx`）：Login + Dashboard、knowledge-bases、documents、knowledge-graph、chat、settings、analytics、multimodal、search、notes、notes/:id、skills、tasks、ops。
- **stores 7 个**：useChat/Document/Graph/Kb/Note/Skill/Task Store。
- **services 8 个**：auth/kb/chart/knowledgeGraph/multimodal/note/skill/taskService，均基于 `@/lib/api` 的 `apiClient`（axios，baseURL `/api/v1`，timeout 60s）。
- **`src/lib/api.ts` 含旧版封装**：`collectionApi`/`documentApi`/`agentApi`/`queryApi`（chat/上传/检索走这里）。
- **端口不一致点**：proxy 8080 vs `lib/api.ts` 注释及既有文档 8000。
- 根目录 6 个 yaml 为 Playwright 页面快照。

### 1.4 已有测试资产（代码确认）

| 资产 | 位置 | 状态 |
|---|---|---|
| pytest 配置 | `backend/pyproject.toml`：`testpaths=["tests"]`、`asyncio_mode=auto`、markers `unit/integration/slow`、coverage `source=["app"]` | ✅ 就绪（无 `api` marker） |
| 存量单测 | `backend/tests/unit/`：agent_memory、elk_logger、plugin_system、token_blacklist、workflow_engine | ✅ 可运行，无 conftest |
| 测试基础设施 | `docker-compose.test.yml`：Redis `26379`、Mongo `27027`、Neo4j `27687/27474`（neo4j/SecureNeo4j@2026，APOC），**无 Milvus** | ✅ 可用 |
| 前端测试依赖 | vitest ^4.1.9、@testing-library/react ^16.3.2、jsdom ^29.1.1、@playwright/test ^1.61.1；scripts `test`/`test:e2e` | ⚠️ 依赖齐备但零配置零用例 |
| 既有测试文档 | `doc/测试计划/全功能测试计划.md`（18 模块、103+ 端点、账号 `e2e_test/Test123456`）+ `功能与接口对照详细文档.md` | ✅ 用例输入 |

### 1.5 差距分析

1. 无统一测试根目录；前端零测试。
2. 集成/API/UI/压力测试空白。
3. Milvus 缺失的降级行为未验证（`repositories/database/milvus_connector.py`）。
4. 响应契约与错误码体系（`core/exceptions.py`）无自动化断言。
5. LLM 内置 Mock 降级未被测试利用。
6. 端口不一致等配置隐患无测试暴露。

### 1.6 关键测试风险（代码复核更新）

- **TestClient 触发 startup 副作用**：startup 初始化 Redis 限流/审计表/ORM/多级缓存/Neo4j，API 测试须保证依赖可用或隔离 startup。
- **LLM 无需 mock**：不配 Key 自动走 `core/mock_data/` 降级；单测 mock `llm_client` 即可。
- **限流默认生效**：`rate_limit_middleware` 全局挂载（依赖 Redis），压测登录会触发 429+Retry-After，纳入断言。
- **异步全链路**：pytest-asyncio 已配置。

---

## 二、目标目录结构（v1.1 修订）

```
test/
├── README.md                    # 运行命令、环境要求、marker 约定
├── pytest.ini                   # 方案 A：独立 pytest 配置（§3.1 P0-2）
├── unit/
│   ├── backend/
│   │   ├── conftest.py          # mock Redis/Milvus/Neo4j/llm_client 共享 fixtures
│   │   ├── agents/  services/  core/  plugins/  workflow/
│   └── frontend/
│       ├── setup.ts
│       ├── components/  stores/  services/
│       └── lib/                 # 新增：api.ts 客户端封装测试
├── integration/
│   ├── conftest.py              # 依赖检查、初始化、每用例清理
│   ├── test_rag_pipeline.py     # Milvus 降级路径为重点用例
│   ├── test_graph_pipeline.py   # 真实 Neo4j：Cypher/schema
│   ├── test_memory_redis.py     # 对话记忆 + Redis 断连降级
│   ├── test_audit_mongo.py
│   ├── test_cache_multi_level.py    # 新增：L1+L2 多级缓存
│   └── test_llm_mock_fallback.py    # 新增：LLM 未配置走 Mock 链路
├── api/
│   ├── conftest.py              # 测试 app、token fixture、startup 隔离
│   ├── test_auth_api.py  test_kb_api.py  test_chat_api.py
│   ├── test_graph_api.py  test_notes_api.py  test_ops_health_api.py
│   ├── test_llm_api.py  test_multimodal_api.py          # 新增
│   ├── test_memory_tasks_api.py  test_skills_websearch_api.py  # 新增
│   └── test_response_contract.py                         # 新增：错误码/契约专项
├── ui/
│   ├── playwright.config.ts  fixtures/  pages/（Page Object）
│   └── specs/
│       ├── auth.spec.ts  chat.spec.ts  knowledge-base.spec.ts
│       ├── notes.spec.ts  knowledge-graph.spec.ts
│       └── skills-tasks.spec.ts  search-analytics.spec.ts  # 新增
└── stress/
    ├── locustfile.py  scenarios/  README.md（基线+运行方法）
```

**与既有资产关系**：`backend/tests/unit/` 存量 5 文件保留不动；pytest 衔接见 P0-2。

---

## 三、各层测试代码生成计划

### 3.1 前置任务（P0）

| # | 任务 | 说明 |
|---|---|---|
| P0-1 | `test/README.md` | 各层运行命令、环境要求、marker 约定、端口约定 |
| P0-2 | pytest 配置衔接（二选一） | **A（推荐）**：`test/` 下新建 `pytest.ini`：`testpaths=unit api integration`、`asyncio_mode=auto`、`python_files=test_*.py`、`python_classes=Test*`、`markers=unit/integration/api/slow`，与 backend 配置隔离互不干扰；**B**：扩展 `backend/pyproject.toml` 的 `testpaths` 并追加 `api` marker（会改变存量 rootdir 行为，需回归存量用例） |
| P0-3 | 前端测试配置 | 新建 `frontend/vitest.config.ts`（mergeConfig 继承 alias/proxy + `test:{environment:"jsdom",setupFiles:"../test/unit/frontend/setup.ts"}`）；新建 `frontend/playwright.config.ts`（baseURL `:5173`，trace retain-on-failure） |
| P0-4 | 统一端口与 .env.test | **解决 8080/8000 不一致**：UI/E2E 前将 `vite.config.ts` proxy 目标改回 `http://localhost:8000`（与 `lib/api.ts` 注释、既有文档一致）。`test/api`、`test/integration` 用 `.env.test`（Redis 26379、Mongo 27027、Neo4j 27687，**不配 LLM Key 启用内置 Mock 降级**） |

#### 3.1.1 关键配置示例（v1.2 新增）

**① `test/pytest.ini`（方案 A，推荐）**

```ini
[pytest]
testpaths = unit api integration
asyncio_mode = auto
python_files = test_*.py
python_classes = Test*
python_functions = test_*
markers =
    unit: 单元测试（无外部依赖，全 mock）
    integration: 集成测试（依赖 docker-compose.test.yml 三件套）
    api: API 接口测试（依赖 docker-compose.test.yml）
    slow: 慢速测试（默认排除，CI 单独跑）
addopts = -v --tb=short -m "not slow"
filterwarnings =
    ignore::DeprecationWarning
```

说明：`testpaths` 相对 `test/` 目录解析；各层 `conftest.py` 置于层目录内实现作用域隔离（`unit/` 的 fixtures 不泄漏到 `integration/`）。

**② `frontend/vitest.config.ts`（v1.2 新增）**

```ts
import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

export default mergeConfig(
  viteConfig, // 继承 @→src alias、proxy、plugins
  defineConfig({
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["../test/unit/frontend/setup.ts"],
      include: ["../test/unit/frontend/**/*.test.{ts,tsx}"],
      css: false,
      restoreMocks: true,
      clearMocks: true,
      coverage: {
        provider: "v8",
        include: ["src/stores/**", "src/services/**", "src/lib/**"],
        thresholds: { lines: 80 },
      },
    },
  }),
);
```

**③ `frontend/playwright.config.ts`（v1.2 新增）**

```ts
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "../test/ui/specs",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "cd frontend && npm run dev",
      url: "http://localhost:5173",
      reuseExistingServer: !process.env.CI,
    },
  ],
});
```

注意：webServer 仅起前端；后端（uvicorn :8000）与 docker 三件套由 §四 的启动脚本负责。

**④ `test/.env.test`（v1.2 新增）**

```bash
# 中间件（对齐 docker-compose.test.yml）
REDIS_URL=redis://localhost:26379/0
MONGO_URI=mongodb://localhost:27027/kquery_test
NEO4J_URI=bolt://localhost:27687
NEO4J_USER=neo4j
NEO4J_PASSWORD=SecureNeo4j@2026

# 关键：不配置 LLM API Key → is_llm_configured()=false → core/mock_data 内置 Mock 降级
# OPENAI_API_KEY=
# DASHSCOPE_API_KEY=

# 测试账号（对齐 doc/测试计划/全功能测试计划.md）
TEST_USER=e2e_test
TEST_PASSWORD=Test123456
```

### 3.2 单元测试 `test/unit/`（P1）

**后端（pytest + pytest-asyncio + unittest.mock，外部依赖全 mock）**

| 目标模块（代码确认） | 测试文件 | 核心用例 |
|---|---|---|
| `services/rag_service.py`、`retriever.py`、`bm25_retriever.py`、`bm25_index_manager.py` | `services/test_rag_service.py`、`services/test_retriever.py` | 混合检索分数融合、召回为空兜底、rerank |
| `services/chunking.py`、`embedding.py`、`hyde_generator.py` | `services/test_chunking.py`、`services/test_embedding.py`、`services/test_hyde.py` | 分块边界/overlap、embedding 维度与批处理、HyDE 改写 |
| `services/file_loaders/`（13 类解析器） | `services/test_file_loaders.py` | 用 `sample_documents/` 样本验证 PDF/Word/Excel/PPT/OCR 解析结构 |
| `services/graph/`（实体抽取、graph_builder、graph_query） | `services/test_graph_builder.py` | 构图、Cypher 生成（mock neo4j driver） |
| `services/graphrag/`（graphrag_engine、qa_service） | `services/test_graphrag_engine.py` | 图增强检索组装、GraphRAG 回答流程 |
| 高级服务 | `services/test_web_search.py`、`test_long_term_memory.py`、`test_task_service.py`、`test_writing_service.py`、`test_image_table_understanding.py` | 联网搜索、记忆分层、任务编排、AI 写作、多模态理解 |
| `agents/qa_agent.py`、`orchestrator.py`、`rule_engine.py`、`rag_tool.py`、`decision_evaluator.py`、`memory.py` | `agents/test_qa_agent.py`、`test_orchestrator.py`、`test_rule_engine.py`、`test_rag_tool.py` | 意图路由、工具选择、规则优先级、工具调用、决策评估 |
| `core/auth.py`、`security.py`、`rbac.py`、`tenant.py`、`kb_isolation.py` | `core/test_rbac.py`、`test_kb_isolation.py`、`test_security.py` | 角色权限矩阵、跨租户拒绝、知识库隔离、上传文件校验 |
| `core/cache.py`（L1+L2）、`cache_warmer.py`、`rate_limiter.py`、`connection_pool.py` | `core/test_cache.py`、`test_rate_limiter.py` | 多级缓存回源与失效、限流窗口与 429、连接池复用 |
| `core/llm_client.py`（**LLM mock 关键点**）、`core/exceptions.py`、`i18n.py`、`audit_logger.py`/`audit_storage.py` | `core/test_llm_client.py`、`test_exceptions.py`、`test_audit.py` | 重试/超时/Mock 降级分支、错误码映射、审计落库 |
| `plugins/manager.py`、`registry.py`、`workflow/engine.py`、`skills/builtin/` | `plugins/test_plugin_manager.py`、`workflow/test_workflow_engine.py`、`test_skills.py` | 插件注册/加载/卸载、异常隔离；工作流执行；内置技能 |
| `models/`（Pydantic 校验） | `test_models_validation.py` | 请求模型边界值、非法入参报错 |

**前端（vitest + Testing Library）**

| 目标 | 测试文件 | 核心用例 |
|---|---|---|
| 7 个 zustand stores | `stores/*.test.ts` | 状态变更、异步 action（mock `@/lib/api`） |
| 8 个 services（auth/kb/chart/graph/multimodal/note/skill/task） | `services/*.test.ts` | 请求路径/参数组装、错误分支 |
| **`src/lib/api.ts`（agentApi/documentApi/queryApi/collectionApi，v1.1 补充）** | `lib/api.test.ts` | chat/上传/检索请求封装、`response.data.data ?? response.data` 兼容逻辑 |
| 组件与工具函数 | `components/*.test.tsx`、`utils.test.ts` | 渲染/交互/受控状态、纯函数边界值 |

**验收**：后端核心服务行覆盖率 ≥ 70%（复用 pyproject coverage 配置）；前端 stores/services/lib 全覆盖。

#### 3.2.1 后端单元用例明细（v1.2 新增，含用例 ID 便于追踪）

| 用例 ID | 文件 | 用例名 | 验证点 |
|---|---|---|---|
| UT-RAG-01 | services/test_rag_service | 混合检索分数融合 | 向量+BM25 分数加权归一化正确 |
| UT-RAG-02 | services/test_rag_service | 召回为空兜底 | 无命中返回空结果而非异常 |
| UT-RAG-03 | services/test_rag_service | rerank 生效 | 重排后 top-k 顺序正确 |
| UT-RET-01 | services/test_retriever | 多路检索合并去重 | 重复文档按策略去重 |
| UT-CHK-01 | services/test_chunking | 分块边界 | 按 token/句子边界切分符合配置 |
| UT-CHK-02 | services/test_chunking | overlap 生效 | 相邻块重叠满足参数 |
| UT-EMB-01 | services/test_embedding | 批处理与维度 | mock 返回维度一致、分批上限 |
| UT-HYD-01 | services/test_hyde | 查询改写 | 问题→Mock 假设文档→检索 |
| UT-FILE-01 | services/test_file_loaders | PDF 解析 | `sample_documents/` 样本：文本/元数据 |
| UT-FILE-02 | services/test_file_loaders | Word/Excel/PPT 解析 | 表格结构保留 |
| UT-FILE-03 | services/test_file_loaders | OCR 图片解析 | mock OCR 输出接入 |
| UT-GRA-01 | services/test_graph_builder | 实体关系构图 | 抽取→节点/边写入（mock driver） |
| UT-GRA-02 | services/test_graph_builder | Cypher 生成 | 模板正确、参数化防注入 |
| UT-GRAG-01 | services/test_graphrag_engine | 图增强检索组装 | 社区摘要+子图拼接 |
| UT-WS-01 | services/test_web_search | 结果归一化 | 多源返回字段统一 |
| UT-LTM-01 | services/test_long_term_memory | 记忆分层存取 | 长期/短期记忆读写与过期 |
| UT-TSK-01 | services/test_task_service | 任务编排执行 | 依赖顺序、失败重试 |
| UT-WRI-01 | services/test_writing_service | AI 写作流程 | Mock LLM 下生成结构正确 |
| UT-IMG-01 | services/test_image_table_understanding | 表格理解 | 图片→结构化表格（mock LLM） |
| UT-SPK-01 | services/test_speech | 语音合成 | mock TTS 返回音频字节 |
| UT-QA-01 | agents/test_qa_agent | 意图路由 | 问题→正确工具/agent |
| UT-QA-02 | agents/test_qa_agent | 工具选择 | mock llm_client 下工具调用链 |
| UT-ORC-01 | agents/test_orchestrator | 编排流程 | 多步任务串行/并行正确 |
| UT-RUL-01 | agents/test_rule_engine | 规则优先级 | 命中最高优先级规则 |
| UT-RAG-05 | agents/test_rag_tool | 工具调用参数 | 检索工具入参组装正确 |
| UT-RBAC-01 | core/test_rbac | 角色权限矩阵 | 各角色可/不可访问矩阵断言 |
| UT-KBI-01 | core/test_kb_isolation | 跨租户拒绝 | 租户 A 无法读租户 B 知识库 |
| UT-SEC-01 | core/test_security | 上传文件校验 | 非法类型/超限被拒 |
| UT-TKN-01 | core/test_token | token 生命周期 | 生成/刷新/黑名单生效 |
| UT-CCH-01 | core/test_cache | L1+L2 回源 | 未命中逐级回源并回填 |
| UT-CCH-02 | core/test_cache | 失效传播 | L1 失效触发 L2 回源 |
| UT-RL-01 | core/test_rate_limiter | 限流窗口 | 超阈值拒绝、窗口重置 |
| UT-LLM-01 | core/test_llm_client | 重试与超时 | tenacity 重试 N 次后抛错 |
| UT-LLM-02 | core/test_llm_client | Mock 降级分支 | is_llm_configured=false 返回 mock 数据 |
| UT-EXC-01 | core/test_exceptions | 错误码映射 | 异常→ErrorCode→HTTP 状态 |
| UT-AUD-01 | core/test_audit | 审计落库 | 关键操作写审计（mock storage） |
| UT-PLG-01 | plugins/test_plugin_manager | 插件加载/卸载 | 注册、启用、异常插件隔离 |
| UT-WF-01 | workflow/test_workflow_engine | 工作流执行 | 节点顺序、条件分支 |
| UT-SKL-01 | skills/test_skills | 内置技能执行 | 各内置技能输出正确 |
| UT-MDL-01 | models/test_models_validation | 边界值校验 | 非法入参触发 VAL_002 |

**前端用例明细（v1.2 新增）**

| 用例 ID | 文件 | 验证点 |
|---|---|---|
| UT-FS-01~07 | stores/*.test.ts（7 个 store） | 状态变更、异步 action、异常回滚（mock `@/lib/api`） |
| UT-FSVC-01~08 | services/*.test.ts（8 个 service） | 请求路径/参数/headers、错误分支与超时 |
| UT-FLIB-01~04 | lib/api.test.ts | agentApi 对话、documentApi 上传、queryApi 检索、collectionApi 管理；`response.data.data ?? response.data` 兼容逻辑 |
| UT-FC-01 | components/（表单类） | 受控输入、校验提示 |
| UT-FC-02 | components/（列表类） | 加载/空态/错误态渲染 |
| UT-FUTIL-01 | utils.test.ts | 纯函数边界值（格式化/解析） |

### 3.3 集成测试 `test/integration/`（P2）

前置：`docker compose -f docker-compose.test.yml up -d`（Redis/Mongo/Neo4j）；**不配置 LLM Key**（启用内置 Mock 降级）。

| 测试文件 | 链路 | 说明 |
|---|---|---|
| `test_rag_pipeline.py` | 上传→chunking→embedding→检索→问答 | **Milvus 不可用降级路径为重点用例** |
| `test_graph_pipeline.py` | 实体抽取→真实 Neo4j（27687）→图谱查询→GraphRAG | 验证 Cypher 与 schema |
| `test_memory_redis.py` | 对话记忆写入/读取/过期（26379） | 含 Redis 断连降级到内存用例 |
| `test_audit_mongo.py` | 审计日志写 Mongo（27027）→查询 | 字段完整性 |
| `test_cache_multi_level.py` | L1 内存 + L2 Redis 多级缓存一致性 | 缓存预热（cache_warmer）与失效传播 |
| `test_llm_mock_fallback.py` | `is_llm_configured()=false` 全链路 Mock 降级 | 验证 qa/embeddings/hyde/graph/image 五类 Mock 数据 |

`conftest.py`：检查容器健康→初始化测试租户/用户/知识库→每用例后 flush 测试库。

#### 3.3.1 集成链路步骤（v1.2 新增）

| 测试文件 | 步骤（Given-When-Then） | 断言 |
|---|---|---|
| test_rag_pipeline | Given 测试文档（sample_documents/）→ When 上传+chunking+embedding(Mock)+检索 → Then 问答 | 分块数>0；检索命中 top-k；回答包含知识片段 |
| test_rag_pipeline（降级） | Given Milvus 不可用（不启动 milvus_connector）→ When 检索 → Then 走降级（BM25/向量近似或明确报错） | 不抛未处理异常；日志记录降级 |
| test_graph_pipeline | Given 实体抽取(Mock) 结果 → When 写入真实 Neo4j(27687) → Then Cypher 查询/GraphRAG | 节点/关系数量正确；Cypher 返回结构符合 schema |
| test_memory_redis | Given 会话 → When 写入记忆(26379) → Then 读取/过期 | 内容一致；TTL 到期后取空 |
| test_memory_redis（断连） | Given Redis 不可达 → When 记忆读写 → Then 降级内存存储 | 不崩溃；内存内一致 |
| test_audit_mongo | Given 业务操作 → When 写审计(27027) → Then 查询 | 记录数+1；字段完整（时间/用户/动作） |
| test_cache_multi_level | Given 预热 cache_warmer → When 查 L1/L2 → Then 一致性 | 命中率；失效传播及时 |
| test_llm_mock_fallback | Given 不配 Key → When 调用 qa/embeddings/hyde/graph/image 五类链路 → Then Mock 数据 | 返回结构与真实模型兼容 |

**conftest.py 关键 fixtures（v1.2 设计）**：`docker_ready`（探测 26379/27027/27687 端口，超时 30s 失败即 skip）、`test_tenant`/`test_user`（初始化租户与账号 `e2e_test/Test123456`）、`test_kb`（建临时知识库）、`cleanup`（autouse，用例后 flush 测试库/删测试图）。

### 3.4 API 测试 `test/api/`（P2）

方式：`httpx.AsyncClient` + ASGITransport 直连 `app.api.main:app`；数据库走 docker-compose.test.yml；**startup 副作用需处理**（真实依赖可用，或 override startup 事件/mock 外部连接）。用例来源：`doc/测试计划/功能与接口对照详细文档.md`（103+ 端点）。

**响应契约断言规则（v1.1 重写，替代原 code/message/data 说法）**：
- 成功：断言 HTTP 状态码 + `response_model` 关键字段与类型（如 `/login` → 200 + `access_token/refresh_token/token_type`；`/register` → 201）。
- 失败：断言统一错误结构 `error.code / error.message / request_id` 及语义（`AUTH_*`、`RES_*`、`VAL_002`、`RATE_LIMIT_EXCEEDED`）；429 断言 `Retry-After`、`X-RateLimit-*` 头。
- 统一在 `conftest.py` 提供 `assert_error` / `assert_success` 辅助 + `auth_headers` fixture（login 取 token）。

| 测试文件 | 覆盖路由（代码确认） | 重点用例 |
|---|---|---|
| `test_auth_api.py` | `auth_router`（register/login/refresh/me） | 注册 201、登录、refresh、无 token 401、错误密码 4xx |
| `test_kb_api.py` | `knowledge_base_router` | 共享知识库 CRUD、跨租户 403 |
| `test_chat_api.py` | `router.py` + `agent_router.py` + `ws_manager.py` | 流式问答、会话上下文、WebSocket 协议 |
| `test_graph_api.py` | `knowledge_graph.py`、`graphrag.py` | 图谱查询参数校验、GraphRAG 响应结构 |
| `test_notes_api.py` | `notes_router.py`、`mindmap_router.py` | 笔记 CRUD、思维导图生成（Mock LLM） |
| `test_ops_health_api.py` | `health.py`、`ops_router.py`、`/metrics` | `/health` 探活、依赖降级健康上报、Prometheus 指标 |
| `test_llm_api.py`（新增） | `llm_router.py` | 多模型切换、Mock 降级响应 |
| `test_multimodal_api.py`（新增） | `multimodal.py` | 多模态内容生成校验 |
| `test_memory_tasks_api.py`（新增） | `memory_router.py`、`tasks_router.py` | 长期记忆、任务式 Agent |
| `test_skills_websearch_api.py`（新增） | `skills_router.py`、`web_search_router.py`、`speech_router.py` | Skills 执行、联网搜索、语音 |
| `test_response_contract.py`（新增） | 全量抽查 | 错误码体系、429 限流头、404/422 语义 |

#### 3.4.1 API conftest 设计（v1.2 新增）

| fixture | 职责 |
|---|---|
| `client` | 基于 `httpx.AsyncClient(transport=ASGITransport(app))` 的异步客户端；setup 中处理 startup（依赖可用时正常启动；不可用时 patch/override startup 事件，仅挂载路由） |
| `auth_headers` | 调用 `/login` 获取 token → 返回 `{"Authorization": "Bearer <token>"}`（session 级缓存复用） |
| `test_user`/`test_tenant` | 注册独立测试账号与租户（每模块隔离） |
| `assert_success` | 断言 HTTP 状态 + 响应模型关键字段（成功契约） |
| `assert_error` | 断言 `error.code/message/request_id` 结构 + HTTP 状态（失败契约） |
| `ratelimit_off` | 压测类用例临时关闭限流或调大阈值（避免误伤） |

#### 3.4.2 API 用例矩阵（v1.2 新增，选取每文件代表用例）

| 用例 ID | 文件 | 请求 → 断言 |
|---|---|---|
| API-AUTH-01 | test_auth_api | POST /register → 201 + 新用户可登录 |
| API-AUTH-02 | test_auth_api | POST /login 错误密码 → 4xx + error.code=AUTH_* |
| API-AUTH-03 | test_auth_api | 无 token 访问 /me → 401 |
| API-AUTH-04 | test_auth_api | refresh 换新 token → 200 + 旧 token 失效 |
| API-KB-01 | test_kb_api | 创建/查询/删除共享知识库 → CRUD 契约 |
| API-KB-02 | test_kb_api | 跨租户读共享知识库 → 403 |
| API-CHAT-01 | test_chat_api | POST /agent/chat 流式 → SSE 分片、结束标志 |
| API-CHAT-02 | test_chat_api | WebSocket 会话 → 消息往返协议正确 |
| API-GRAPH-01 | test_graph_api | 图谱查询参数校验 → 非法参数 422/VAL_002 |
| API-GRAPH-02 | test_graph_api | GraphRAG 问答 → 响应结构含答案/来源 |
| API-NOTE-01 | test_notes_api | 笔记 CRUD + 思维导图生成(Mock) → 结构正确 |
| API-OPS-01 | test_ops_health_api | GET /health → 200 + 依赖状态字段 |
| API-OPS-02 | test_ops_health_api | /metrics → Prometheus 文本格式 + 指标存在 |
| API-LLM-01 | test_llm_api | 多模型切换参数 → Mock 响应兼容 |
| API-MM-01 | test_multimodal_api | 多模态内容生成 → 返回内容/URL 字段 |
| API-MEM-01 | test_memory_tasks_api | 长期记忆写入/读取 → 记忆分层生效 |
| API-TSK-01 | test_memory_tasks_api | 任务创建/状态查询 → 任务流正常 |
| API-SKL-01 | test_skills_websearch_api | Skills 执行 → 技能结果返回 |
| API-WS-01 | test_skills_websearch_api | 联网搜索 → 结果归一化字段 |
| API-CT-01 | test_response_contract | 不存在路由 → 404 + error 结构 |
| API-CT-02 | test_response_contract | 高频请求 → 429 + Retry-After/X-RateLimit-* 头 |

### 3.5 UI 测试 `test/ui/`（P3）

框架 Playwright（依赖已装）。前置：启动后端（8000）+ 前端 dev（5173）+ 测试基础设施，**并先完成 P0-4 端口统一**。账号 `e2e_test/Test123456`。根目录 6 个 yaml 快照（chat/kb/main/notes/skills/tasks）作为页面结构断言参考。

Page Object 模式，spec 与前端 15 页面路由对应：

| spec | 覆盖页面 | 关键场景 |
|---|---|---|
| `auth.spec.ts` | Login | 登录成功跳转 Dashboard、错误提示 |
| `chat.spec.ts` | Chat | 发送消息、流式渲染、图片上传、会话切换 |
| `knowledge-base.spec.ts` | KnowledgeBases、Documents | 创建知识库、上传文档（`sample_documents/`）、状态流转 |
| `notes.spec.ts` | Notes、NoteEditor | 新建笔记、AI 写作、思维导图展示 |
| `knowledge-graph.spec.ts` | KnowledgeGraph | 图谱渲染、节点交互 |
| `skills-tasks.spec.ts`（新增） | Skills、Tasks | 技能列表、任务创建与状态 |
| `search-analytics.spec.ts`（新增） | SearchResults、Analytics | 检索结果展示、图表面板 |
| （可选）`ops-center.spec.ts` | OpsCenter | 指标面板、告警列表 |

#### 3.5.1 UI 场景步骤（v1.2 新增）

**Page Object 设计要点**：每个 PO 类封装"页面定位 + 操作 + 核心断言"。例：

```ts
// pages/ChatPage.ts（示意）
export class ChatPage {
  constructor(private page: Page) {}
  get input() { return this.page.getByPlaceholder("输入问题..."); }
  get messages() { return this.page.locator("[data-testid=chat-message]"); }
  async send(text: string) { await this.input.fill(text); await this.page.keyboard.press("Enter"); }
  async expectMessage(text: string) { await expect(this.messages.filter({ hasText: text })).toBeVisible(); }
}
```

| spec | 场景步骤（Given-When-Then） | 断言 |
|---|---|---|
| auth.spec.ts | Given 打开 /login → When 输入 `e2e_test/Test123456` 提交 → Then 跳转 Dashboard | URL=/dashboard；导航可见 |
| auth.spec.ts（失败） | Given 错误密码 → When 提交 → Then 错误提示可见 | 错误文案；停留 /login |
| chat.spec.ts | Given 已登录进入 /chat → When 输入问题发送 → Then 流式消息渲染 | 消息出现；无 loading 残留 |
| chat.spec.ts（图片） | When 上传图片 → Then 图片缩略图出现并可发送 | 附件组件状态 |
| kb.spec.ts | Given /knowledge-bases → When 新建知识库 → Then 列表出现新项 | 名称匹配；状态=就绪 |
| kb.spec.ts（上传） | When 上传 `sample_documents/*.pdf` → Then 文档状态流转至已完成 | 进度→成功；可检索 |
| notes.spec.ts | Given /notes → When 新建笔记并保存 → Then 列表与编辑器同步 | 标题/内容一致 |
| graph.spec.ts | Given 有知识库数据 → When 打开 /knowledge-graph → Then 图谱画布渲染节点 | 节点数量>0；无错误 toast |
| skills-tasks.spec.ts | When 启用技能/创建任务 → Then 状态正确 | 技能开关生效；任务状态流转 |
| search-analytics.spec.ts | When 输入检索词 → Then 结果列表 + 图表面板加载 | 结果非空；canvas 存在 |

**fixtures 设计**（`test/ui/fixtures/`）：`auth.ts` 提供已登录 `storageState`（复用 login 后 cookie/token），spec 声明 `test.use({ storageState })` 跳过重复登录，大幅提速。

### 3.6 压力测试 `test/stress/`（P3）

框架 Locust（Python，与后端同生态，新增 dev 依赖）。**LLM 场景无需真实模型**（内置 Mock 降级）。

| 场景文件 | 压测目标 | 关键指标 |
|---|---|---|
| `scenarios/chat_load.py` | `/agent/chat` 并发问答 | P95/P99、RPS、流式首 token 延迟 |
| `scenarios/search_load.py` | `/kquery/*` 检索 + 图谱查询 | 检索 P95、缓存命中率 |
| `scenarios/upload_load.py` | 文档批量上传解析（Celery 队列） | 吞吐、失败率、队列积压 |
| `scenarios/auth_burst.py` | 登录突发流量 | **429 限流触发行为**、错误率、Retry-After 头 |

`locustfile.py` 聚合场景；`README.md` 写明基线（首跑后标定，如问答 P95 < 3s @ 50 并发）与运行命令。

#### 3.6.1 压力场景参数（v1.2 新增）

| 场景 | 初始并发 | 峰值并发 | 时长 | 关键指标 | 初版基线（待首跑标定） |
|---|---|---|---|---|---|
| chat_load | 20 | 50 | 5min | P95/P99、RPS、流式首 token | P95 < 3s，错误率 < 1% |
| search_load | 20 | 80 | 5min | P95、缓存命中率 | P95 < 1.5s，命中率 > 60% |
| upload_load | 5 | 20 | 5min | 吞吐、失败率、Celery 队列积压 | 吞吐 > 5 docs/min，失败率 < 2% |
| auth_burst | 10 | 100（突发） | 3min | 429 行为、错误率、Retry-After 头 | 限流生效且无 5xx |

**运行方式**：

```bash
cd test/stress
locust -f locustfile.py --host http://localhost:8000 --headless \
  -u 50 -r 5 -t 5m --csv results/chat   # 示例：50 并发 5 分钟
```

说明：`locustfile.py` 按 `--scenario` 参数（或环境变量）选择聚合场景；压测前确保后端开启（内置 Mock 降级即可，无需真实 LLM）。

---

## 四、环境准备与运行命令（v1.2 新增）

### 4.1 环境准备步骤

```bash
# 1) 启动测试基础设施（Redis/Mongo/Neo4j）
docker compose -f docker-compose.test.yml up -d

# 2) 后端依赖（backend 目录已有 pyproject.toml）
cd backend && pip install -e ".[dev]" && cd ..

# 3) 后端测试环境变量（不配 LLM Key → 内置 Mock 降级）
export $(cat test/.env.test | xargs)

# 4) 启动后端（API/UI 测试需要）
uvicorn app.api.main:app --app-dir backend --port 8000 &

# 5) 前端依赖与 dev server（UI 测试需要；先完成 P0-4 端口统一）
cd frontend && npm install && npm run dev &
```

### 4.2 各层运行命令（写入 test/README.md）

| 层 | 命令（在 `test/` 目录） | 前置 |
|---|---|---|
| 后端单元 | `pytest -m unit` | 无（全 mock） |
| 后端集成 | `pytest -m integration` | docker 三件套（§4.1 step1） |
| API | `pytest -m api` | docker 三件套 + `.env.test` |
| 前端单元 | `cd frontend && npx vitest run` | 无 |
| UI/E2E | `cd frontend && npx playwright test` | 前后端运行 + docker |
| 压力 | `locust -f test/stress/locustfile.py --host http://localhost:8000` | 后端运行 |

### 4.3 覆盖率统计（可选）

```bash
cd test && pytest -m unit --cov=backend/app --cov-report=term-missing --cov-fail-under=70
```

---

## 五、实施里程碑与验收标准（v1.2 细化）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| M1 | P0 前置（目录、配置、端口统一、README、.env.test） | `pytest --collect-only` 可发现 0 用例不报错；vitest/playwright 配置可加载 |
| M2 | 单元测试（后端→前端） | 后端覆盖率 ≥ 70%；前端 stores/services/lib 全覆盖；存量 5 个用例不回归 |
| M3 | 集成 + API 测试 | 集成 6 文件全绿（含 Milvus 降级、Redis 断连、Mock 降级 3 个关键路径）；API 契约用例全绿 |
| M4 | UI 测试 | 7 个 spec 在 chromium 全绿；yaml 快照基线可复用 |
| M5 | 压力测试 | 首跑产出基线 CSV 并回填 §3.6.1；无 5xx、限流生效 |

**补充说明**
- 新测试遵循存量风格：class-based `Test*`、`@pytest.mark.asyncio`、`AsyncMock`。
- LLM 调用统一走 `core/llm_client.py` 内置 Mock 降级，单测层 mock `llm_client`，禁止访问真实大模型 API。
- 响应契约以 v1.1 规则为准（§3.4），不再依赖 code/message/data 包裹假设。
- CI 接入为后续可选项：unit/api 层适合 PR 门禁，integration/stress 适合定时任务。
- 依赖登记：后端新增 dev 依赖 `pytest-asyncio`（若未含）、`httpx`、`pytest-cov`、`locust`；前端依赖已在 package.json（vitest/testing-library/playwright/jsdom）。

