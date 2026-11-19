# 贡献指南

感谢参与。本文只写**这个仓库特有的约定**，通用流程（fork / branch / PR）不赘述。

## 1. 环境准备

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| Python | 3.12 | 后端（`backend/pyproject.toml` 锁定 `>=3.11,<3.13`） |
| uv | 最新 | 依赖管理与运行 |
| Node.js | 22 | 前端与 e2e |
| Docker | 最新 | 起 Milvus / Redis，或跑全栈 |

```bash
cd backend && uv sync && cp .env.example .env
cd frontend && npm install          # postinstall 会创建 tests/node_modules 链接
cd .. && cp backend/.env.example backend/.env   # 本地已存在则跳过
```

无模型 API Key 也能开发：`MODEL_PROVIDER` 留 `auto` 时会自动回落 `mock`，功能链路可跑通，只是答案质量不可评估。

## 2. 目录约定（强约束）

| 想放什么 | 放哪里 | 不要放 |
| --- | --- | --- |
| 测试用例 | `tests/unit` `tests/integration` `tests/format` `tests/contract` `tests/security` `tests/e2e` `tests/e2e/ui` | 业务代码目录下的 `tests/` |
| 前端单测 | `tests/frontend/` | `frontend/` 内的任何测试目录（旧位置已迁移） |
| 需要真实服务的验证脚本 | `tests/e2e/scripts/` | `scripts/` 根 |
| 运维脚本（备份/演练/容量/压测） | `scripts/` | `tests/` |
| 评测运行器 | `scripts/eval/` | 根目录 |
| 部署文件（Dockerfile / compose / nginx / k8s） | `deploy/` | `backend/`、`frontend/`、仓库根 |
| 面向使用者的文档 | `wiki/` | `doc/` |

测试数据只放 `tests/data/`，禁止在用例里内联大段语料。运行产物（`report*.json`、`runs_*.jsonl`）不要提交。

## 3. 代码规范

### 后端

- `ruff check` + `ruff format` 必须干净；行宽、import 顺序等已在 `pyproject.toml` 规定。
- 分层边界：`api/` 不写业务；`services/` 不 `import fastapi`；`core/` 不反向依赖 `services/`。`tests/unit/test_import_boundaries.py` 会拦住越界。
- 错误用 `core/envelope.py` 的 `ErrCode` 常量，不要抛裸 `HTTPException(500)`。
- 新增配置项：只改 `backend/app/core/config.py`，然后运行 `python scripts/gen_env_example.py` 重新生成 `.env.example`（CI 会校验）。
- 新增端点：同步 `tests/contract/` 的 schema 断言与前端 `frontend/src/lib/api-client.ts` 封装。

### 前端

- `npm run typecheck:test` 与 `npm run build` 都必须通过（`tsc -b` 是构建的一部分）。
- 按 feature 组织（`frontend/src/features/{chat,knowledge,admin,shell,common}/`），跨 feature 复用的逻辑放 `frontend/src/lib/`。组件不直接 `fetch`：HTTP 走 `frontend/src/lib/api-client.ts`，流式走 `frontend/src/lib/sse-client.ts`。
- 单测写行为不写实现：断言用户可见结果或 store 状态变化，不断言内部函数调用次数。

### 文档

- 文档只写**当前代码事实**，不写"计划支持"；未实现的能力放 README 的已知限制。
- 路径引用用相对仓库根的反引号形式，`scripts/check_doc_refs.py` 会校验存在性。
- 改动公开文档后跑 `python scripts/check_doc_refs.py`。

## 4. 提交前检查

```bash
uv run --project backend python -m pytest
uv run --project backend ruff check backend tests scripts
cd frontend && npm run typecheck:test && npm test && npm run build
cd .. && python scripts/check_doc_refs.py
```

- 改了检索/生成/分块逻辑，必须附评测对比（`scripts/eval/run_eval.py`，基线口径见 [wiki/05](./wiki/05-测试与评测.md)）。只有"感觉更好了"的 PR 不会被合。
- 新增依赖要说明为什么不能用现有依赖；前端优先零依赖实现（当前运行时依赖只有 react / react-dom / react-router-dom / marked）。

## 5. PR 要求

- 一个 PR 只做一件事。重构与行为变更分开，否则评审无法判断哪部分改变了结果。
- 描述里写清：**问题 → 方案 → 验证方式**。涉及配置或接口变更时，列出兼容性影响。
- 不提交 `.env`、密钥、真实业务文档、运行产物与向量库卷。
- 新增/删除文件时确认没有把 `deploy/milvus/volumes/`、`data/`、`node_modules/` 之类本地状态带进提交。

## 6. 报告问题

请附带：复现步骤、期望与实际、`trace_id`（响应头或响应体里）、相关日志片段（`docker compose logs backend`）、以及 `GET /healthz` 的输出。缺这些信息的问题很难定位。
