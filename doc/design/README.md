# Kquery 设计文档索引（未实现项的目标态设计）

> - **定位**：本目录回答「**还没实现的功能，长什么样**」。
> - 与其它目录的分工：
>
> | 目录 | 回答的问题 | 时态 |
> |---|---|---|
> | `doc/analysis/` | 系统**现在**有什么能力、怎么工作、有哪些缺口 | 现在（现状） |
> | `doc/plan/` | **做过什么**计划、每条做到哪一步、为什么这么做 | 过去 + 进度 |
> | `doc/design/` | **还没做的事**目标态长什么样、怎么落地、怎么验收 | 未来（设计） |
>
> - **代码基线**：见 `doc/analysis/README.md` §1.5（本目录不单独钉 SHA）
> - **状态口径**（全目录统一，四态）：✅ **已实现** · ⚠️ **部分实现** · ❌ **未实现** · 🚫 **已下线**
> - **未实现项标记**（全目录统一）：**未实现项编号**沿用源计划编号（`A-xx` 代码规范 / `C-xx` 并发 / `K-xx` 知识库 / `B0 未修` 来自 `_B0-缺口复评清单.md`），**不重新编号**，避免同一件事在两处出现两个号。

---

## 〇、一句话定位

Kquery 的**已落地能力**相当完整（89 端点、58 个后端测试文件、974 个用例、13 个能力域全覆盖），**真正缺的不是功能，而是三样东西**：**可度量的质量**（无检索金标集）、**收敛的错误体系**（29 处裸异常 + 47 处兜底 `except`，口径：`backend/app/core` + `backend/app/services`，不含 `main.py` 与 `api/`）、**可自动验证的工程纪律**（无 lint 规则集、无 CI 护栏、无 import 边界测试）。这三样决定了「已实现的能力能不能被证明正确」。

---

## 一、本目录文档清单

| 文件 | 覆盖的未实现项 | 源计划 | 主题 |
|---|---|---|---|
| `01-知识库与授权模型设计.md` | K1~K5 | `doc/plan/知识库实体与授权-分析与实施计划.md` §十四 | KB 授权粒度（用户 → 角色/群组）、租户级探测修正、`kb_enabled` 与 `acl_enabled` 解耦、移库重授权 |
| `02-检索质量度量设计.md` | B0 KQ-09 / X-07 / X-08 / KQ-16 | `_B0-缺口复评清单.md` | 金标集构造、评测指标（recall@k / nDCG / MRR）、跑批流水线、与 `14-差距与演进.md` S1 对齐 |
| `03-错误体系与可观测性设计.md` | A-07 ~ A-11、A-15 ~ A-18 | `doc/plan/代码规范符合性审计与整改计划.md` §8.2 | 领域异常基类、裸异常收敛、错误指标、保留码裁决、日志纪律 |
| `04-模块边界与依赖倒置设计.md` | A-12 ~ A-14 | 同上 | `main.py` 拆分、分层规则、import 边界 AST 测试 |
| `05-并发提容与降级阶梯设计.md` | C-01 ~ C-13 | `doc/plan/单体内并发能力提升-分析与实施计划.md` §9.1 | 压测基线落盘、隔离仓观测、降级阶梯完整化、**真流式（TTFT）专项** |

---

## 二、未实现项总表（跨计划汇总 · 2026-10-30 复核）

> 这是本目录的**核心索引**。每条都指向具体设计文档的章节，**不重复写方案**。

### 2.1 正确性与安全（P0）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|
| K1 | **KB 授权只到用户粒度** | `backend/app/services/security/kb.py:48`、`:239-275` | `01` §二 | `PUT /admin/kb/{id}/grants` 支持 `role:` / `group:` 表达式，与文档 ACL 同构 |
| K2 | **KB 空结果探测是 tenant 级** | `backend/app/services/pipeline.py:406-423` | `01` §三 | 探测带 KB 允许清单；「无内容」与「无权限」可区分（测试断言两种 reason 不同） |
| K3 | **`kb_enabled` 与 `acl_enabled` 强耦合** | `backend/app/dependencies.py:82-83` | `01` §四 | `acl_enabled=false` + `kb_enabled=true` 可独立生效 |
| K4 | **批量移库不重授权** | `backend/app/api/kb.py:214-242`、`backend/app/services/knowledge.py:155-164` | `01` §五 | 移库时返回「受影响授权」清单，或提供「同步迁移授权」选项 |
| A-05 | **`/healthz` 回显异常原文** | `backend/app/main.py:832-836` | `03` §五 | 断网探测时响应体不含连接串 / 路径 / 堆栈 |
| A-11 | **图存储损坏从空图启动** | `backend/app/services/graph/sqlite_store.py:10` | `03` §六 | 损坏文件 → 拒绝启动并给出修复指引 |
| B0 KQ-26 | **证据区不扫描敏感词** | `backend/app/services/pipeline.py:930-944`、`backend/app/services/security/sensitive.py:95` | `03` §七 | 证据含敏感词 → refuse 隐藏 / mask 替换 |
| B0 KQ-27 | **提示词注入面** | `backend/app/services/pipeline.py:64-89` | `02` §五 | 证据块有显式分隔 + 系统提示声明「数据不是指令」 |

### 2.2 可度量性（P0/P1）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|
| B0 KQ-09 | **无检索金标集** | 全仓无 `recall@k` / `nDCG` / `MRR` 计算代码 | `02` §二~§四 | `scripts/eval_retrieval.py` 可跑批并输出三指标；金标集入库且有 CI 冒烟 |
| B0 KQ-16 | **上下文硬截断不可达** | `backend/app/services/context.py:124,152` | `02` §六 | 截断路径可达且有回归测试覆盖 |
| A-09 | **无 `kquery_errors_total`** | `backend/app/core/metrics.py` 无该指标 | `03` §三 | `/metrics` 暴露；集成测试触发 ≥3 个 `kind` |
| A-10 | **13 个保留错误码是死位** | `backend/app/core/envelope.py:12-107` | `03` §四 | 逐码裁决：接线或移除，不留死位 |

### 2.3 工程纪律（P0/P1）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|
| A-01 | **ruff 无 `select` 规则集** | `backend/pyproject.toml` `[tool.ruff.lint]` 为空 | `03` §一（前置） | 规则集选定 + 存量告警基线可复现 |
| A-02 | **CI 无 `discipline` job** | `.github/workflows/ci.yml` 仅 4 个 job | `03` §一（前置） | 插入违规 → CI 变红 |
| A-03 | **`backend/.gitignore` 不存在** | `git check-ignore backend/app/.env` 未命中 | `03` §一（前置） | 命中 |
| A-04 | **生产未禁用 `/docs`** | `backend/app/main.py:707` | `03` §二 | `curl /docs` → 404 |
| A-06 | **已完成入库任务无淘汰** | `backend/app/services/ingest.py:78` 只限队列 | `03` §八 | 连续上传 600 文档后任务表长度稳定 |
| A-07 | **29 处裸 `raise RuntimeError/ValueError`** | `backend/app/core` + `services` | `03` §二 | 降到基线 30% 以下 |
| A-08 | **47 处 `except Exception:`** | `backend/app/` | `03` §二 | ≤ 3（仅进程边界） |
| A-19 | **无 `gen_env_example.py`** | `scripts/` 无该文件 | `03` §九 | 生成物与仓库 `.env.example` 逐行一致 |
| A-20 | **无「写端点必须限流」断言** | `backend/tests/` 无此类断言 | `03` §九 | 漏挂即红 |
| B0 X-30 | **无 `tests/security/` 与 `tests/contract/`** | `backend/tests/` 仅 4 层 | `02` §七 | 两个目录建立且有用例 |
| B0 X-25 | **CI 缺 image / 冒烟 / 覆盖率阈值** | `.github/workflows/ci.yml` | `03` §九 | 四个 job 补齐 |

### 2.4 架构与可观测性（P1/P2）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|
| A-12 | **`main.py` 899 行** | `backend/app/main.py` | `04` §二 | < 200 行；lifespan < 60 行 |
| A-13 | **无 import 边界测试** | `backend/tests/unit/test_import_boundaries.py` 不存在 | `04` §三 | AST 断言「core 不 import services/api」 |
| A-14 | **30 处顶层 `import app.`** | `backend/app/**` | `04` §三 | 归零或全部带 `# 破环` 注释 |
| A-15 | **78 处 `logging.getLogger`** | `backend/app/**` | `03` §八 | 收敛到统一工厂 |
| A-16 | **启动无脱敏配置回显** | `backend/app/core/logging_setup.py` | `03` §八 | 日志中可见，API key 显示 `***` |
| A-17 | **无 `nodes={...}` 阶段耗时汇总** | 全仓 `nodes=` 只有 2 处命中且都与日志无关：`backend/app/services/chunkers/factory.py:79` 的 `max_top_nodes`、`backend/app/api/graph.py:148` 的图谱响应字段 | `03` §八 | 一次 `/ask` 日志中可见 |
| A-18 | **`extra` 字段不截断** | `backend/app/core/logging_setup.py` | `03` §八 | 10KB payload 入日志被截断到 512 |
| B0 X-27 | **两个上帝对象** | `services/pipeline.py` 1291 行、`main.py` 899 行 | `04` §二 | 抽纯函数与阶段协作者 |

### 2.5 并发与容量（P1）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|
| C-01 | **压测无多租户编排** | `scripts/load_test.py:185,231` 单账号 | `05` §一 | 聚合吞吐能区分租户 |
| C-02 | **压测基线数据不落盘** | `doc/analysis/并发容量基线.md` 不存在 | `05` §一 | 每次压测追加一行 |
| C-03 | **§1.1 数字未在批次 1 后重测** | 2026-10-19 旧口径 | `05` §一 | 新旧口径可对照 |
| C-04 | **客户端断开不主动取消** | `request.is_disconnected` 全仓 0 命中 | `05` §二 | 断开后 2s 内释放线程 |
| C-05 | **指标无延迟采样** | `backend/app/core/metrics.py` | `05` §二（低优先） | 内存占用有上界 |
| C-06 | **`api-client.ts` 无 429/503 倒计时** | `frontend/src/lib/api-client.ts` 只处理 401 | `05` §二 | 管理面背压体验与 SSE 一致 |
| C-07 | **无 `upstream_saturation` gauge** | `backend/app/core/metrics.py` | `05` §三 | 许可占用率可见 |
| C-08 | **无图/检查点锁等待指标** | `backend/app/services/graph/sqlite_store.py` | `05` §三 | 锁等待 P95 可读 |
| C-09 | **图写入未后台化** | `backend/app/services/graph/builder.py` | `05` §三 | 读路径延迟不随写入上升 |
| C-10 | **降级阶梯无「上游 P95」判据** | `backend/app/core/config.py:271` 无消费点 | `05` §四 | P95 超阈值时 CRAG 轮次收缩 |
| C-11 | **队列深度不参与拒绝决策** | `backend/app/core/metrics.py:103` 只暴露不消费 | `05` §四 | 深度超阈值拒入库 / 图谱 |
| C-12 | **无生成降级模板** | `backend/app/services/pipeline.py` | `05` §四 | 熔断打开时返回可读降级结果 |
| C-13 | **真流式（TTFT）未实现** | `backend/app/services/pipeline.py:1203,1252` 切片重放 | `05` §五 | TTFT < 全链路耗时；holdback 协议见 §五 |

---

## 三、推进顺序建议

```
第一梯队（安全 / 数据正确性，1~2 周）
  A-01~A-03  工具地基 ──┐  ← 没有它，下面所有项都无法自动验证
  A-04 A-05 A-11        │
  K2 K3                 │
        ↓
第二梯队（质量可度量，2~3 周）—— 依赖第一梯队的 CI 护栏
  金标集（02） → 检索质量才有基线 → 才谈得上 A-17 的 nodes 汇总
  A-09 A-10 错误指标与死码裁决（03）
        ↓
第三梯队（工程收敛，3~4 周）—— 依赖前两梯队的验证手段
  A-07 A-08 A-15~A-18 错误体系与日志（03）
  A-12~A-14 结构拆分（04）
  C-07~C-12 隔离仓观测与降级阶梯（05）
        ↓
第四梯队（增量能力，1~2 周）
  K1 K4 授权粒度与移库重授权（01）
  C-01~C-06 压测与前端背压（05）
  C-13 真流式（05 §五）—— 风险最高，单独排期
```

> **反模式警告**：不要先做 A-12~A-14（结构拆分）再做 A-07~A-09（错误收敛）——拆完再收敛，等于把同一个错误处理复制到多个新模块。**先收敛，再拆分。**

---

## 四、文档纪律

| # | 规则 |
|---|---|
| D1 | 本目录**只写未实现项**。已实现的能力 → `doc/analysis/`；已做过的计划 → `doc/plan/`。重复描述现状等于制造第二份真相源。 |
| D2 | 每条未实现项必须给出：**现状证据（`文件:行号`）** · **目标态** · **落点文件** · **验收方式**。缺一不可。 |
| D3 | 设计文档**不钉代码基线 SHA**，一律写「见 `doc/analysis/README.md` §1.5」。 |
| D4 | 未实现项编号**沿用源计划**，不重新编号；跨计划汇总见 §二。 |
| D5 | 任何一条未实现项被实现后：① 本目录对应文档标注「已实现 + 实测日期」；② `doc/plan/` 对应计划的批次表更新状态；③ `doc/analysis/14-差距与演进.md` 的缺口台账移入「已修」。**三处必须同步**。 |
| D6 | 禁止绝对路径（`/Users/`、`C:\`）；路径相对仓库根。 |
| D7 | 本目录不放「计划 / 排期 / 人力」——那是 `doc/plan/` 的职责。本目录只回答「**长什么样**」。 |

---

## 维护触发

- 本目录必须更新的条件（触发任一即需复核）：
  - `doc/analysis/14-差距与演进.md` 的缺口台账变化
  - `doc/plan/` 任一计划的状态列变化
  - `backend/app/core/envelope.py`（错误码枚举）、`backend/app/domain/schemas.py`（领域模型）
  - `backend/app/services/security/kb.py`、`backend/app/dependencies.py`（权限判定）
  - `backend/app/services/pipeline.py`、`backend/app/services/pipeline_executor.py`、`backend/app/services/upstream.py`（并发与降级）
  - `backend/app/main.py`（装配结构）
- **2026-10-30**：本目录建立，汇总 `doc/plan/` 四篇计划 + `_B0-缺口复评清单.md` 的全部机械可验未实现项共 **48 条**（另有 40 条依赖人工通读或真实环境压测的未修项暂未逐条设计，见 `_B0-缺口复评清单.md` §七）。
