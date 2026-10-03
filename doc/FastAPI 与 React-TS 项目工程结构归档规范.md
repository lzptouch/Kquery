# FastAPI 与 React + TS 项目工程结构归档规范

> 本文整理自网络公开资料（检索日期 2026-10-02，来源见文末），归纳 FastAPI 后端与 React + TypeScript 前端两类项目的**目录结构、分层职责、依赖规则与代码归档约定**，作为 Kquery 项目后续阶段（阶段 2 ~ 阶段 10）代码演进的归档依据。
>
> 与 `series/` 十阶段文档的关系：系列文档规定"做什么、为什么"，本文规定"代码放哪、怎么归档"。

---

## 一、FastAPI 后端项目结构

### 1.1 推荐目录树（官方 "Bigger Applications" 模式 + 社区主流扩展）

```
backend/
├── pyproject.toml              # uv 依赖清单与锁文件（uv.lock 必须入库）
├── .env.example                # 环境变量样例（真实 .env 不入库）
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py                 # 应用入口：创建 app、注册路由、中间件、异常处理、lifespan
│   ├── dependencies.py         # 共享依赖（Depends 注入的集中定义处）
│   │
│   ├── api/                    # 路由层（公开端点）：每资源一文件
│   │   ├── ask.py              #   APIRouter(prefix/tags)，只做请求接收与响应
│   │   └── knowledge.py
│   ├── internal/               # 内部非公开端点（如 admin 管理接口），与公开 API 隔离
│   │
│   ├── core/                   # 横切关切：配置、安全、信封、错误码、追踪、日志
│   │   ├── config.py           #   配置单一入口（pydantic-settings 读 .env）
│   │   ├── envelope.py
│   │   └── tracing.py
│   │
│   ├── domain/                 # 领域模型：Pydantic schema（请求/响应/实体校验）
│   │   └── schemas.py
│   ├── models/                 # （引入关系库后）ORM 模型，与 domain schema 分离
│   ├── services/               # 业务逻辑层：与路由解耦，接收依赖对象做核心处理
│   │   ├── chunking.py
│   │   ├── embeddings.py
│   │   └── pipeline.py
│   └── db/                     # （引入数据库后）连接管理与迁移
│       ├── database.py         #   engine / SessionLocal / Base
│       └── migrations/
│
├── tests/                      # 与 app/ 平级，按模块拆分 test_<模块>.py
│   ├── conftest.py             # 公共 fixture（测试客户端、临时数据目录）
│   ├── test_chunking.py
│   └── test_api.py
│
└── data/                       # 运行时产物（索引/上传文件），不入库
```

### 1.2 各层职责与硬性规则

| 层 | 职责 | 硬性规则 |
| --- | --- | --- |
| `main.py` | 组装应用：lifespan 中构建依赖图，注册路由/中间件/异常处理器 | **只组装，不写业务**；依赖在 lifespan 构建后挂到 `app.state` |
| `api/`（routers） | 接收请求、参数校验、调用 service、返回响应 | 路由函数**不直接含业务逻辑**；统一走响应信封与错误码 |
| `internal/` | 管理/内部端点 | 与公开端点物理隔离，便于独立鉴权 |
| `domain/schemas.py` | 请求/响应/实体的 Pydantic 模型 | **字段按"将来要进数据库"的标准设计**；演进只加不改 |
| `services/` | 业务逻辑核心（切块、检索、流水线） | 依赖全部经构造函数注入，**不在模块级实例化外部资源** |
| `core/config.py` | 配置唯一入口 | 环境变量读取只允许出现在这里，其他模块一律 import |
| `db/` | 连接与迁移 | 会话用生成器依赖（`yield` + `finally close`）保证请求结束释放 |
| `tests/` | 单元 + 集成测试 | 与 `app/` 平级；fixture 集中 `conftest.py`；测试用注入替换外部依赖 |

### 1.3 依赖注入的组织方式

- 共享依赖集中定义（`dependencies.py` 或 lifespan 装配），路由通过 `Depends` 获取；
- 外部资源（模型客户端、向量库、时钟）一律走**协议/接口抽象**，生产装配真实实现、测试装配 Mock——这是 FastAPI 社区与 Kquery 阶段 1 共同验证的模式；
- 配置外置：阈值、模型名、提示模板全部走 `core/config.py` + `.env`，代码中零硬编码。

### 1.4 社区补充共识（来自多篇文章的交叉验证）

1. **高内聚低耦合**：按"资源/能力"切分模块，而不是按"技术类型"堆砌巨型文件（如单个 2000 行的 `routers.py` 是典型反模式）；
2. **tests 与 app 平级**：保证测试不进入部署产物，且导入路径清晰；
3. **`.env.example` 入库、`.env` 不入库**：`uv.lock` 必须入库以保证可复现构建；
4. **失败显式化**：全局异常处理器统一转响应信封，错误码分区段，不允许裸 500 透出堆栈。

---

## 二、React + TypeScript 前端项目结构

> 以社区公认模板 **bulletproof-react**（GitHub 17k+ stars，Vite + React + TS）为主线，结合 2025 年多篇实践文章归纳。

### 2.1 推荐目录树

```
frontend/
├── package.json / .nvmrc / vite.config.ts / tsconfig.json
├── index.html
├── public/                     # 静态资源（不参与构建）
└── src/
    ├── main.tsx                # 入口：渲染根组件
    ├── App.tsx                 # 根组件：路由 + 全局 Provider 组装
    │
    ├── app/                    # 应用层：路由表、全局 Provider、布局壳
    │   ├── routes/
    │   └── providers/
    │
    ├── components/             # 跨 feature 共享的通用组件（atoms/通用 molecules）
    │   ├── Button/
    │   └── Modal/
    │
    ├── config/                 # 前端运行时配置（环境变量读取唯一入口）
    ├── hooks/                  # 跨 feature 共享 hooks（useDebounce 等）
    ├── lib/                    # 第三方库的封装层（api-client、auth、storage）
    │   └── api-client.ts       #   单一 API 客户端实例，全应用复用
    ├── stores/                 # 跨 feature 全局状态（zustand 等）
    ├── types/                  # 跨 feature 共享类型（API 契约类型）
    ├── utils/                  # 纯函数工具（无副作用、无 React 依赖）
    ├── assets/                 # 参与构建的静态资源（图片/字体）
    ├── testing/                # 测试工具（render 封装、mock 数据工厂）
    │
    └── features/               # ★ 业务代码主体：按特性垂直切分
        ├── chat/               #   一个特性一个文件夹，内部自包含
        │   ├── api/            #     该特性的 API 请求声明（基于 lib/api-client）
        │   ├── components/     #     该特性私有组件
        │   ├── hooks/          #     该特性私有 hooks
        │   ├── stores/         #     该特性状态
        │   ├── types/          #     该特性类型
        │   └── index.ts        #     特性公开出口（只导出对外暴露的部分）
        └── knowledge/
```

### 2.2 核心原则

**原则 1：feature 垂直切分是主体。** 业务代码的默认归属是 `features/<特性>/` 内部（api/components/hooks/stores/types 自包含），而不是全局平铺。判断标准：**"删掉这个特性，这个文件夹应该能整体删掉"**。

**原则 2：单向代码库（unidirectional codebase）。** 依赖方向只能自上而下：

```
共享层（components / hooks / lib / stores / types / utils）
        ↑ 只允许被引用，不允许反向依赖
特性层（features/*）
        ✕ features 之间禁止互相 import
```

跨 feature 复用某个模块时，把它**上提到共享层**（重构信号：出现第二次需要即上提）。此规则用 ESLint `import/no-restricted-paths` 强制，CI 拦截违规。

**原则 3：单一 API 客户端。** 全应用只有一个 `lib/api-client.ts`（封装 baseURL、拦截器、错误归一化、统一信封解析）；各 feature 在 `features/<x>/api/` 中只做**声明式请求定义**（一个端点一个函数），不在组件里直接写 `fetch`。

**原则 4：状态按生命周期分层归档。**

| 状态层级 | 存放位置 | 示例 |
| --- | --- | --- |
| 应用级（稳定） | `src/stores/` | 主题、当前用户、租户 |
| 会话级（可分享） | **URL 参数** | 当前对话 id、筛选条件 |
| 特性级 | `features/<x>/stores/` | 特性的服务端缓存/本地状态 |
| 视图/临时级 | 组件内部 `useState` | 弹窗开关、输入草稿、悬停态 |

临时态进全局 store 是常见性能事故来源（高频更新引发全量重渲染）。

**原则 5：组件分层归档。**

```
原子组件（src/components/）       ← 无业务语义：Button、Tag、Skeleton
业务组件（features/<x>/components/）← 有业务语义：MessageBubble、EvidenceCard
页面（app/routes/）                ← 只做数据组装与布局，不写展示细节
```

### 2.3 TypeScript 归档约定

- API 契约类型集中 `src/types/`（前后端共享语义的字段名与后端 schema 对齐）；feature 私有类型留在 `features/<x>/types/`；
- 严格模式（`strict`、`noUnusedLocals`）常开；导出用具名导出，feature 通过 `index.ts` 控制公开面；
- 环境变量经 `import.meta.env` 在 `src/config/` 单点读取并导出类型化常量，组件不直接读环境变量。

---

## 三、对照 Kquery 现状与演进指引

### 3.1 当前结构符合度

| 规范项 | Kquery 现状（阶段 1） | 符合度 |
| --- | --- | --- |
| 后端 api/services/core/domain 分层 | `app/api` `app/services` `app/core` `app/domain` 已落地 | ✅ |
| 配置单一入口 | `app/config.py`（pydantic-settings + `.env`） | ✅ |
| 依赖注入（模型/存储可替换） | lifespan 装配 + 协议抽象 + Mock 实现 | ✅ |
| tests 与 app 平级 | `backend/tests/` + `conftest.py` | ✅ |
| 前端共享/特性分层 | `src/components` 通用件 + `src/pages` 页面 + `src/api` 封装 | ✅（阶段 1 规模下 pages 平铺合理） |
| 统一 API 客户端 | `src/api/client.ts` + `src/api/sse.ts` | ✅ |
| 契约类型对齐 | `src/types.ts` 与后端 schema 对齐 | ✅ |

### 3.2 后续阶段的归档落点（新增代码放哪里）

| 阶段 | 后端归档 | 前端归档 |
| --- | --- | --- |
| 2 混合检索/缓存/限流 | `services/retrieval/`（bm25/rrf/rerank 各一模块）、`services/cache.py`、`core/metrics.py` | 演进为 `features/chat/` 内加 `RetrievalTracePanel`；`features/monitor/` 新增指标卡片 |
| 3 智能分块/多模态 | `services/parsers/`（按类型路由）、`services/tasks.py`（入库任务） | `features/knowledge/` 加 `ChunkPreview` `ParserProgressBar` |
| 4 Query 预处理 | `services/query/`（intent/hyde/fusion/decompose） | `features/chat/` 加 `QueryRewriteDiff` `IntentBadge` |
| 5 自反思 | `services/validation/`（scoring/crag/refusal） | `features/chat/` 加 `ConfidenceBar` `RefusalCard` |
| 6 Agent 编排 | `services/agent/`（stategraph/tools/registry） | `features/chat/` 加 `AgentTraceTimeline` `ToolCallCard` |
| 7 图谱/跨模态 | `services/graph/`、`services/multimodal/` | `features/graph/` 新增 |
| 8 多智能体 | `services/multiagent/`（roles/blackboard/critic） | `features/reports/` 新增 |
| 9 企业护栏 | `core/auth.py`、`api/internal/`（admin）、`db/`（审计/配额表） | `features/admin/` 新增 |
| 10 部署运维 | 仓库根 `deploy/`（Dockerfile/K8s manifests） | `features/ops/` 新增 |

**触发重构的两个信号**（出现即执行，不拖延）：

1. 前端 `src/pages/` 超过 3 个页面或某页面内私有组件超过 5 个 → 迁移到 `features/` 垂直结构；
2. 后端某 `services/*.py` 超过约 400 行或出现"多能力共用一个模块" → 按上表拆为子包。

---

## 四、参考来源

**FastAPI**：
- FastAPI 官方教程《Bigger Applications - Multiple Files》（routers/internal/dependencies 模式的官方出处）：https://fastapi.tiangolo.com/tutorial/bigger-applications/
- Structuring a FastAPI Project: Best Practices（DEV Community, 2025-03）：https://dev.to/mohammad222pr/structuring-a-fastapi-project-best-practices-53l6
- FastAPI 目录架构最佳实践（CSDN, 2025-09）：https://blog.csdn.net/m0_50037012/article/details/151687983
- 告别混乱代码：FastAPI 项目的 10 个架构级最佳实践（CSDN, 2025-09）：https://blog.csdn.net/gitblog_01059/article/details/151082050

**React + TypeScript**：
- bulletproof-react 项目结构规范（alan2207/bulletproof-react，GitHub）：https://github.com/alan2207/bulletproof-react/blob/master/docs/project-structure.md
- bulletproof-react 架构深度解析（知乎）：https://zhuanlan.zhihu.com/p/569633590
- bulletproof-react API 层架构：单一 API 客户端实践（CSDN, 2026-09）：https://blog.csdn.net/gitblog_00634/article/details/154107382
- React 架构深度解析与项目管理策略（百家号, 2026-01）：https://baijiahao.baidu.com/s?id=1853411837618112160
- React+TypeScript 实战指南：从类型安全到企业级应用架构（CSDN, 2025-09）：https://blog.csdn.net/gitblog_00270/article/details/151808165
