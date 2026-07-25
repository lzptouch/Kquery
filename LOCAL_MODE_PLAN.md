# LOCAL_MODE 实施方案

> 目标：**数据层全部使用本地文件（零 Docker 依赖），账户/认证统一走标准 OIDC/SSO 远程服务**。
> 本文档只描述方案，不含实现代码。

---

## 一、目标

- **数据层**：全部落到本地磁盘文件，启动不再依赖任何 Docker 容器（向量库、图库、缓存、文档库均本地化）。
- **账户层**：下线本地用户名/密码登录，账户身份统一走 **标准 OIDC/SSO**（Authorization Code + PKCE）。

## 二、现状盘点

| 组件 | 当前实现 | 是否需改 | 本地化方向 |
|---|---|---|---|
| 主库 | SQLite `kquery.db`（aiosqlite） | 否 | 已本地 ✅ |
| 缓存 | L1 内存 + L2 Redis | 是 | L2 改本地磁盘缓存 |
| 向量库 | Milvus（pymilvus） | 是 | 本地向量库（Chroma） |
| 图库 | Neo4j | 是 | 本地图存储（networkx 落盘 或 Kùzu） |
| 文档库 | MongoDB（默认 `None`） | 是 | 默认本地文档存储适配器 |
| 认证 | 本地 JWT + bcrypt（`core/auth.py`、`api/auth_router.py`） | 是 | 增加 OIDC 分支 |
| 可观测 | Prometheus / ES / Logstash | 否 | 已优雅降级 ✅ |

涉及关键文件：

- `core/settings.py`
- `core/cache.py`
- `repositories/connector.py`
- `repositories/graph/neo4j_connector.py`
- `core/auth.py`
- `api/auth_router.py`
- `api/config_router.py`
- `services/ops/*`

## 三、总体架构

采用 **`LOCAL_MODE` 开关 + 适配器模式**：

- 抽象出 `VectorStore` / `GraphStore` / `DocStore` 接口。
- 运行时按 `LOCAL_MODE` 选择「远程（Milvus / Neo4j / Mongo）」或「本地（文件）」实现。
- 认证按 `OIDC_ENABLED` 选择本地登录或 SSO。

调用方（如 `rag_service.py`、`knowledge_graph.py`、`graph_query.py`）通过同一接口访问，对底层实现无感。

## 四、改造点逐项

### 1. 配置中心（`core/settings.py`）

新增：

- `LOCAL_MODE: bool`
- OIDC：`oidc.enabled` / `oidc.issuer` / `oidc.client_id` / `oidc.client_secret` / `oidc.redirect_uri` / `oidc.scopes` / `oidc.jwks_auto_fetch`
- 本地数据目录：`local_data_dir = ./data`

### 2. 缓存（`core/cache.py`）

- `LOCAL_MODE` 时，L2 改用 `diskcache.Cache(local_data_dir/cache)`；L1 内存保留。
- 非本地模式维持现有 Redis L2。

### 3. 向量库（`repositories/database/`）

- 新增 `vector_store.py`：`VectorStore` 抽象（`upsert` / `search` / `delete`）。
- 实现 `MilvusVectorStore`（现有逻辑迁入）+ `LocalVectorStore`（Chroma `PersistentClient(path=data/chroma)`）。
- 原 `milvus_connector.py` 的 `get_milvus_connector()` 改为按开关返回对应实现。

### 4. 图库（`repositories/graph/`）

- 新增 `graph_store.py`：`GraphStore` 抽象（Cypher 风格查询 → 适配）。
- 实现 `Neo4jGraphStore`（现有）+ `LocalGraphStore`：用 `networkx` 在内存构图，落盘为 `data/graph/graph.pkl`（或 json），开机加载、写时持久化。
- 图算法服务（`graph_query.py` / `community_engine.py`）已基于 networkx，迁移成本最低。
- 备选：Kùzu 嵌入式图库（更贴近 Cypher，体积稍大）。

### 5. 文档库

- 新增 `doc_store.py`：默认 `LocalDocStore`（SQLite / JSONL，落在 `data/docs/`）。
- 已有 MongoDB 分支保留为远程选项（`mongodb_url` 非空时启用）。

### 6. 认证 → OIDC/SSO（`core/auth.py` + `api/auth_router.py`）

- 保留 JWT 机制（用于内部会话），但**身份来源改为 IdP**。
- 新增依赖：`authlib[starlette]` + `httpx` + `PyJWT`。
- 新增路由：
  - `GET /auth/oidc/login`：302 跳转 IdP 授权页。
  - `GET /auth/oidc/callback`：用 `code + PKCE` 交换 `id_token` / `access_token`；按 JWKS 验签；本地建/更新 `UserORM`（不含密码）；签发内部 JWT。
- `get_current_user()`：本地模式且 `OIDC_ENABLED` 时，user 来自 OIDC 会话；`FEATURE_USER_AUTH_ENABLED` 关闭仍走匿名。
- 登录页下线本地注册/密码登录表单。

### 7. 前端（`frontend/`）

- 登录页改为「使用 SSO 登录」按钮，跳转 `/auth/oidc/login`。
- 回调页拿到内部 JWT 后存本地、跳首页。
- 其余接口调用不变（仍带 Bearer Token）。

## 五、文件改动清单

```
core/settings.py                      # +LOCAL_MODE / OIDC 配置 / local_data_dir
core/cache.py                         # L2 走 diskcache
repositories/database/vector_store.py # 抽象 + Local(Chroma) 实现
repositories/database/milvus_connector.py   # 改为按开关返回实现
repositories/graph/graph_store.py     # 抽象 + Local(networkx 落盘) 实现
repositories/graph/neo4j_connector.py # 改为按开关返回实现
repositories/doc_store.py             # 新增 LocalDocStore
core/auth.py                          # +OIDC 验签/会话
api/auth_router.py                    # +/auth/oidc/login /callback
api/config_router.py                  # 暴露 LOCAL_MODE / OIDC 开关状态
frontend/.../Login.tsx                # SSO 按钮 + 回调处理
data/                                 # 新增本地持久化目录（gitignore）
```

## 六、配置示例（`.env`）

```
LOCAL_MODE=true
OIDC_ENABLED=true
OIDC_ISSUER=https://your-idp.example.com
OIDC_CLIENT_ID=xxxx
OIDC_CLIENT_SECRET=xxxx
OIDC_REDIRECT_URI=http://localhost:5173/auth/callback
OIDC_SCOPES=openid profile email
```

## 七、依赖

- `chromadb`
- `diskcache`
- `authlib[starlette]`
- `httpx`
- `PyJWT`
- （图库若选 Kùzu 则额外加 `kuzu`）

## 八、验证 / 测试

1. 起 `LOCAL_MODE=true` + `OIDC_ENABLED=true`，确认启动 **不依赖任何 Docker 容器**。
2. 跑现有 pytest 套件（37 项）应全绿（缓存/向量/图均走本地实现）。
3. 用 agent-browser 走一遍：SSO 登录 → 对话 / 知识库 / 知识图谱 / 运维 7 能力可用。

## 九、实施顺序（建议分 Phase）

- **P1 数据本地化**：配置开关 → 缓存 → 向量 → 图 → 文档（不动认证，先让 Docker 依赖清零）。
- **P2 OIDC 接入**：auth 分支 + 路由 + 前端登录。
- **P3 收尾**：config 暴露状态、文档、测试。

## 十、风险与回滚

- 抽象层保持接口一致，开关默认 `false` → 改动对现有远程部署无影响，可随时回滚到 Milvus / Neo4j / Redis。
- `data/` 目录加入 `.gitignore`，避免把本地数据提交。
