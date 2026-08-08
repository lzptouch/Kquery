# Kquery 测试体系总说明

本目录包含 Kquery 项目的 5 层测试：单元测试、集成测试、API 测试、UI 测试、压力测试。

## 目录结构

```
test/
├── pytest.ini              # pytest 配置（单元/集成/API 三层）
├── .env.test               # 测试环境变量（不配 LLM Key → 内置 Mock 降级）
├── unit/                   # 后端单元测试（pytest）+ 前端单元测试（vitest）
├── integration/            # 集成测试（依赖 docker-compose.test.yml）
├── api/                    # API 接口契约测试（httpx + TestClient）
├── ui/                     # UI/E2E 测试（Playwright）
└── stress/                 # 压力测试（Locust）
```

## 环境准备

```bash
# 1. 启动测试基础设施（Redis/Mongo/Neo4j）
docker compose -f docker-compose.test.yml up -d

# 2. 安装后端依赖（backend 目录已配置）
cd backend && pip install -e ".[dev]" && cd ..

# 3. 加载测试环境变量（关键：不配置 LLM Key，触发内置 Mock 降级）
export $(cat test/.env.test | xargs)

# 4. 启动后端服务（API/UI 测试需要）
uvicorn app.api.main:app --app-dir backend --port 8000 &

# 5. 安装前端依赖并启动 dev server（UI 测试需要）
cd frontend && npm install && npm run dev &
```

## 各层运行命令

| 测试层 | 命令 | 工作目录 | 前置条件 |
|---|---|---|---|
| 后端单元测试 | `pytest -m unit` | `test/` | 无（全 mock） |
| 后端集成测试 | `pytest -m integration` | `test/` | docker 三件套 |
| API 测试 | `pytest -m api` | `test/` | docker 三件套 + 后端启动 |
| 前端单元测试 | `npx vitest run` | `frontend/` | `npm install` |
| UI/E2E 测试 | `npx playwright test` | `frontend/` | 前后端 + docker 三件套 |
| 压力测试 | `locust -f test/stress/locustfile.py --host http://localhost:8000` | `test/stress/` | 后端启动 |

## 覆盖率

```bash
cd test && pytest -m unit --cov=backend/app --cov-report=term-missing --cov-fail-under=70
```

## 标记说明

- `@pytest.mark.unit`：纯内存/mock 测试，无外部依赖
- `@pytest.mark.integration`：依赖 docker-compose.test.yml 中 Redis/Mongo/Neo4j
- `@pytest.mark.api`：端到端接口测试，依赖完整后端服务
- `@pytest.mark.slow`：慢速/耗时测试，默认不执行

## 重要约定

1. **LLM 调用**：不配置 OPENAI/DASHSCOPE 等 Key，系统自动走 `backend/app/core/mock_data/` 内置 Mock 数据。单元测试层可额外 mock `app.core.llm_client` 中的函数。
2. **端口统一**：`frontend/vite.config.ts` 已将 `/api` 代理到 `http://localhost:8000`，确保 UI 测试与 API 测试使用同一端口。
3. **测试隔离**：集成/API 测试的 `conftest.py` 会在每个用例后清理测试数据（Redis DB flush、Mongo 测试集合、Neo4j 测试图）。
