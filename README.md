# Kquery 智能 RAG

生产级智能 RAG 系统（混合检索 + Agentic 编排 + GraphRAG + 多模态 + 企业护栏）。**系统现状分析见 `doc/analysis/`（14 篇正文 + 索引 + 2 篇专题），未实现项的目标态设计与工程规范见 `doc/plan/`（2 篇）。**

## 目录

| 目录 | 说明 | 技术栈 |
| --- | --- | --- |
| `backend/` | RAG 后端服务（`app/{api,core,domain,services,assembly}` 分层） | FastAPI + Milvus/FAISS + 云模型（uv 管理） |
| `frontend/` | Web 前端（`src/{app,components,features,lib,types}`） | Vite + React + TypeScript（nvm node 24） |
| `tests/` | 全部测试（unit / integration / format / contract / security / e2e / e2e_ui） | pytest + playwright |
| `scripts/` | 运维 / 评测 / 巡检脚本（评测套件在 `scripts/eval/`） | Python |
| `deploy/` | 部署清单（k8s / milvus / nginx / 告警规则） | — |
| `doc/analysis/` | 系统现状分析（14 篇正文 + 索引 + 2 篇专题，按能力域编排） | — |
| `doc/plan/` | 未实现项的目标态设计 + 工程规范（文件层次 / 语言 / 写作），2 篇 + 索引 | — |
| `doc/runbook/` | 运维手册（告警 / 备份 / SLA / 分级） | — |

## 文档怎么读

| 你想知道 | 看哪篇 |
|---|---|
| 系统现在有什么能力 / 怎么工作 / 缺什么 | `doc/analysis/`（14 篇正文 + 索引；每篇开头有 **2026-11-12 现状更新**段） |
| 某个机制**应该**怎么设计（目标态 + 落地状态） | `doc/plan/`（2 篇 + 索引） |
| 为什么要这么做、还差什么 | `doc/plan/README.md` §二 未实现项总表（含现状证据与验收） |
| 缺口的台账（哪条已修、哪条没修、证据在哪） | `doc/analysis/14-差距与演进.md`（§3.0.x 逐轮登记） |
| 检索质量怎么量的、数字在哪 | `tests/data/eval/README.md`（评测口径 + 基线 + 图文对抗题） |

> **文档里的数字应当可核对**：`python scripts/doc_facts.py` 从代码抽取端点 / 模块 / 配置 / 指标 / 测试规模，
> 文档数字对着它的输出改，而不是对着记忆改。

## 模型供应商（三个 provider 各自独立）

| 能力 | provider | 说明 |
|---|---|---|
| **生成**（问答 / 追问 / 标题） | `MODEL_PROVIDER` = `auto` \| `dashscope` \| `deepseek` \| `minimax` \| `zhipu` \| `mock` | 三家都是 OpenAI 兼容的 `/chat/completions`（`DEEPSEEK_BASE_URL` / `MINIMAX_BASE_URL` / `ZHIPU_BASE_URL`）。`auto` 顺序：**DeepSeek → MiniMax → 智谱 → 千问 → mock** |
| **向量化** | `EMBEDDING_PROVIDER` = `auto` \| `dashscope` \| `minimax` \| `zhipu` \| `openai_compatible` \| `mock` | `auto` 跟随生成 provider；**DeepSeek 没有 embeddings 端点**（实测 404）→ 回落 `dashscope`；**MiniMax 有** `embo-01`（1536 维，请求体是 `texts`/`type`）→ 跟着走；**智谱有** `embedding-3`（2048 维，OpenAI 形状）→ 跟着走。维度按模型自动校准，**换 provider 必须重建集合** |
| **重排** | `RERANK_PROVIDER` = `auto` \| `dashscope` \| `deepseek` \| `zhipu` \| `openai_compatible` \| `mock` | `deepseek` = **对话模型做 listwise 重排**（走 `/chat/completions`，多一次 LLM 往返）；`zhipu` = 真 `/rerank` 端点（`results[].relevance_score`）；`openai_compatible` 走 `{base}/rerank`（Jina / Cohere / 硅基流动风格） |
| **OCR** | `OCR_PROVIDER` = `auto` \| `dashscope` \| `mock` | 只有千问 `qwen-vl-ocr` 是 `/ocr` 协议形状。智谱 `glm-4.5v` 是**对话式多模态**（走 VLM 直答），强行做版面 OCR 会丢表格结构 |

**智谱的思考开关**：`glm-4.5` 系默认开思考，思考 token 计入 `max_tokens`——预算小时
`finish_reason=length` 且**正文为空**（RAG 拿到空答案但不报错）。本系统默认下发
`thinking: {"type": "disabled"}`，需要推理链时设 `ZHIPU_THINKING_ENABLED=true`。

**要用别家的 embeddings / rerank**：把 provider 改成 `openai_compatible` 并填
`EMBEDDING_BASE_URL` / `RERANK_BASE_URL`（**必须是提供这两个端点的服务**，
DeepSeek 直连会 404）。

> **换 `MODEL_PROVIDER` 时注意模型名**：`LLM_MODEL` / `INTENT_MODEL` / `AGENT_PLANNER_MODEL`
> 留着**千问出厂名**（`qwen-*`）会自动替换成目标 provider 的默认模型；
> 但如果你**显式写过**别的名字（例如 `.env` 里钉了 `LLM_MODEL=deepseek-chat`），
> 再切到 `zhipu` 就会把这个名字原样发给智谱 → **400**。「用户显式配置优先」是有意为之
> （否则无法在 DeepSeek 上用非默认模型），代价是切换 provider 时要一起改模型名。

**模型降级**（`LM-01`，默认关）：`LLM_FALLBACK_ENABLED=true` + `LLM_FALLBACK_MODELS=备选1,备选2`
→ 主模型失败且**可降级**（超时 / 限流 / 5xx）时换模型；鉴权与参数错**不降级**。
降级在响应里可见（`model_used` / `routing_degraded`）。

## 快速开始

```bash
# 向量库（Milvus standalone，etcd + 本地存储）
cd deploy/milvus && docker compose up -d

# 后端（8000 端口）
cd backend && cp .env.example .env   # 填入 DASHSCOPE_API_KEY 或 DEEPSEEK_API_KEY；留空自动用 Mock 模型
uv sync && uv run uvicorn app.main:app --port 8000

# 前端（5173 端口，代理到 8000）
cd frontend && nvm use && npm install && npm run dev
```

打开 http://localhost:5173 → 「知识库」上传 PDF/Markdown → 「对话」提问，答案中带 `[#N]` 引用，点击可跳转证据。

## 测试

```bash
uv sync --project backend --extra dev            # 必须带 --extra dev：playwright 在 extra 里，不在 dev 组
uv run --project backend python -m pytest        # 全部测试（Mock 模型，无需 API Key）
uv run --project backend python -m pytest --cov  # 带覆盖率（门槛 80%，实测 85%）
uv run --project backend python -m ruff check backend/app tests   # 静态检查
```

> **仓库布局（2026-11-12 整理）**：`tests/` 与 `data/` 在**仓库根**（与 `backend/` 平级），
> 依赖与虚拟环境仍在 `backend/`。pytest / ruff 的配置在**根** `pyproject.toml`
> ——配置留在 `backend/` 会被 pytest 完全忽略（超时保护、覆盖率门槛一起失效）。
>
> 命令一律用 `python -m pytest` / `python -m ruff`（**不用** venv 里的 console script）：
> console script 的 shebang 写死了创建时的绝对路径，仓库换个位置就 "bad interpreter"。

> `uv sync` **默认不装 `dev` extra**（playwright / pytest-playwright 在 extra 里），
> 而 `tests/e2e_ui/` 的用例要 import playwright。
> 少装这一步时 `uv run pytest` 会在**收集阶段**就报
> `ModuleNotFoundError: No module named 'playwright'`，整片红——
> 看着像测试坏了，其实是依赖没装全。

当前 **2051 个用例**（后端 unit 1135 / integration 473 / format 389 / contract 10 / security 10 —— 这五层共 **2017 条免外部依赖**；另有 e2e 16 条与 e2e_ui 18 条需运行中的服务与浏览器；含 389 条格式专项）+ **109 个前端用例**（vitest，17 个文件）+ 浏览器端到端巡检脚本（`scripts/e2e_full.py`），覆盖率门槛 85%。CI 见 `.github/workflows/ci.yml`（Linux 上跑真实 Milvus Lite，Windows 自动回退 FAISS）。

检索评测（recall@k / nDCG@10 / MRR，见 [`tests/data/eval/README.md`](./tests/data/eval/README.md)）：
**检索金标 120 条 + 问答金标 60 条**（26 篇语料 / 463 块）与扩容后基线已落地
（mock 向量口径：recall@10 = 0.9545 / nDCG@10 = 0.836；旧 20 条基线 recall@10 = 0.75，两者不可比），
CI 有回退门禁（劣化 > 2pp 失败）。另有**切块策略实验**（6 组）、**索引重建脚本**（沙箱 → 比对 → 原子切别名）、
**Agent 过程评测**（工具选择准确率 / 平均迭代 / 失控率）。

**图文（金标集）**：12 张真实开源图片 + 3 份开源 README（BSD/MIT/PSF，来源见
`tests/data/eval/corpus_img/SOURCES.md`），常规 16 条 + **对抗 11 条**（答案只在原图里）。
实测：检索 recall@10 = 1.00、**文本可答性 = 0.00** → 瓶颈在"看图"不在"找图"。

```bash
# 本地复算（零外部依赖）
uv run --project backend python scripts/eval/run_eval.py --mode local \
  --queries tests/data/eval/queries.jsonl --qrels tests/data/eval/qrels.tsv \
  --runs tests/data/eval/runs.local.jsonl --out tests/data/eval/report.local.json
# 线上采集（真实 embedding，需凭据）：--mode live --base-url http://127.0.0.1:8100 --user admin --password '***'
```

格式专项（**50 个支持扩展名**逐一过通用解析契约，口径见 [`doc/analysis/06-入库与解析链路.md`](./doc/analysis/06-入库与解析链路.md)）：

```bash
uv run --project backend python -m pytest tests/format -v
```

浏览器 UI 用例默认打 `127.0.0.1:5173`（vite dev server）。
打 Docker 部署的前端用：

```bash
KQUERY_UI_WEB=http://127.0.0.1:8090 uv run --project backend python -m pytest tests/e2e_ui -v
```

## 阶段进度

- [x] 阶段 1：Naive RAG 基线（切块 / 向量检索 / 引用溯源 / SSE 流式 / 统一信封 / 错误码）
- [x] 阶段 2：混合检索（BM25 + 向量 + RRF + gte-rerank-v2）与工程化（语义缓存 / 令牌桶限流 / Prometheus 指标 / 监控大盘 / 检索配置）
- [x] 阶段 3：智能分块与多模态（Recursive/Semantic/ParentChild/RAPTOR 四策略 + PDF 表格 / qwen-vl-ocr + 入库任务队列 + 解析预览）+ Milvus 向量库 —— 语音转写（原 `paraformer-v2`）已于 2026-11-14 整体下线，音频扩展名改为明确提示
- [x] 阶段 4：Query 预处理（qwen-turbo 意图识别 / Fusion 改写 / HyDE / Step-Back / 多跳拆解 / 数据源路由 + 闲聊短路 + 分析记录可回放）
- [x] 阶段 5：自反思与自校正（CRAG 三分支 + 证据评分 + 幻觉自检 + 受限重生成 + 显式拒答 + 置信度 + 拒答反馈工单 + 质量指标）
- [x] 阶段 6：Agentic RAG（LangGraph 状态机 Plan/Tool/Reflect/Replan/Answer + 工具注册表与幻觉兜底 + 三层熔断 + 多轮会话存档 + 执行轨迹可视化）
- [x] 阶段 7：GraphRAG 与多模态（qwen-plus 实体关系抽取 + 消歧归一 + 内存图 BFS + 稳定编号社区摘要 + qwen-vl-max 结构化描述统一向量空间 + graph_search/image_search 工具 + 图谱浏览器）
- [x] 阶段 8：多智能体报告（Planner/Researcher/Validator/Writer/Critic 五角色 + Blackboard 黑板 + 拓扑分层并行 + Writer↔Critic 评分循环 + 报告中心）—— **已于 2026-10-17 下线**（下线记录见 `doc/plan/实施记录-doc-qa能力整合.md` 与 `doc/analysis/14-差距与演进.md` §六）
- [x] 阶段 9：企业级护栏 —— **认证与授权已整体下线（2026-11-12）**：注册/登录/口令/JWT/用户目录/角色门禁/文档级 ACL/知识库授权全部删除，请求身份恒为 `default/local`；**访问边界改由部署层承担**（内网 / 反向代理 / 网关）。仍保留：突发令牌桶限流、危险级工具人工审批、日志 PII 脱敏、`/metrics` 的 X-Metrics-Token 校验。评测平台、敏感词、配额、审计日志此前亦已下线。
- [x] 整合 doc-qa 独有能力（2026-10-25；能力现状见 `doc/analysis/01-能力全景.md`，目标态见 `doc/plan/`）：
  Markdown 富文本渲染（marked + `[#N]` 角标）/
  扩展解析格式（.xlsx + .pptx + 纯文本 + 代码文件）/ 单文档「快问」通道（临时索引 + TTL + 范围隔离 + 会话维度）/
  引文增强（参考汇总卡片 / 引文搜索 / 导出 TXT）/
  OCR **只走云服务 API**（不提供本地或自部署模型路径，已用守卫测试钉死）/
  推荐追问与会话自动命名 / 共享存储 Redis 化（缓存 + 限流 + 快问临时态，Fail-Open 降级）
- [x] 阶段 10（部署演进）：`OP-01` on-call runbook（8 张处置卡 + SLO + 备份脚本 + 恢复演练）✅ · `OP-02` 灰度自动回滚 ✅ · `OP-03` 部署层 canary ✅（`OP-02` / `OP-03` 的**规则集与目标环境压测**仍待补）

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
| 单租户问答稳态 | 60/min（令牌桶） | 成本护栏，设计值 |

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
- 治理能力：突发令牌桶限流 + 危险级工具人工审批 + 日志 PII 脱敏 + `/metrics` 令牌校验（审计/配额/敏感词/认证/ACL/知识库授权均已下线；访问边界由部署层承担）
- doc-qa 能力整合：Markdown 富文本渲染 / 扩展格式（.xlsx .pptx 纯文本 代码）/ 单文档「快问」通道 / 引文增强（参考卡片 / 引文搜索 / 导出 TXT）/ 共享存储 Redis 化
