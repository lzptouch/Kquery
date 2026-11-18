# Kquery 设计文档索引（未实现项的目标态设计）

> - **定位**：本目录回答「**还没实现的功能，长什么样**」。
> - 与其它目录的分工：
>
> | 目录 | 回答的问题 | 时态 |
> |---|---|---|
> | `doc/analysis/` | 系统**现在**有什么能力、怎么工作、有哪些缺口 | 现在（现状） |
> | `doc/plan/` | **还没做的事**目标态长什么样、怎么落地、怎么验收 | 未来（设计） |
> | `doc/testing/` | 测试计划与验收编排（**待建**，见 `01` §2.1.6） | 待建 |
>
> - **代码基线**：见 `doc/analysis/README.md` §1.5（本目录不单独钉 SHA）
> - **状态口径**（全目录统一）：⚠️ **部分实现**（有机制但有明确缺口）· ❌ **未实现**（整项缺失）· 🚫 **已下线**
> - **未实现项标记**（全目录统一）：**未实现项编号**沿用源计划编号（`A-xx` 代码规范 / `C-xx` 并发 / `B0 未修` 来自 `_B0-缺口复评清单.md`），**不重新编号**，避免同一件事在两处出现两个号。源计划目录 `doc/plan/` 与 `_B0-缺口复评清单.md` **均已撤销**，编号仅作历史沿用——**编号指向的内容以本目录为准**，不要再去磁盘上找那份清单。

---

## 〇、一句话定位

Kquery 的**已落地能力**相当完整（70 端点、126 个后端测试文件、1809 个用例、D1~D10 十个能力域全覆盖），**真正缺的不是功能，而是三样东西**：**可度量的生成质量**（检索侧已能量化，生成侧从零开始）、**收敛的错误体系**（裸异常已从 39 处收敛到 **4 处**，且由 `tests/unit/test_bare_exception_count.py` 守卫「≤ 12 处」；广口径 `except Exception` 仍按「必须记日志 + 有降级语义」逐点审查）、**可自动验证的工程纪律**（写端点限流断言、`.env.example` 漂移检测、import 边界测试与错误指标均已落地）。

**检索侧与工程纪律的第一轮已闭合**（金标集 + 指标层 + CI 回退门禁 + lint 规则集 + `test-quality` 静态护栏），**生成侧整体为零**——这是本目录现在的主战场。

本目录现由**两篇**组成：前者管「**代码应该写成什么样**」（规范 · 错误体系 · 结构 · 并发），后者管「**能力应该长成什么样**」（上下文工程 · 生成质量 · 模型调用面 · 运维发布）。

> **2026-11-12 更新**：认证与权限管理整套下线（注册 / 登录 / 口令 / JWT / 用户目录 / 角色门禁 / 文档级 ACL / 知识库授权）。因此本目录**不再包含任何认证或权限相关的设计项**——访问边界改由部署层承担（反向代理 / 内网 / 网关）。原 `01-知识库与授权模型设计.md` 随之下线并删除。

---

## 一、本目录文档清单

| 文件 | 覆盖的未实现项 | 源计划 | 主题 |
|---|---|---|---|
| `01-工程基线与质量保障设计.md` | **0 条**（`A-12` / `B0 X-27` / `C-09` / `C-04` / `C-13` / `A-26` / `C-05` 已于 2026-11-13 落地，见 §2.4 / §2.5 收尾说明） | `doc/plan/`（已撤销）整合（原代码规范审计 + 单体并发 + B0 复评清单） | **工程规范基线（文件层次规范 · 语言与命名规范 · 文档写作规范 · 可量化基线 · 依赖方向）** · ~~`main.py` 拆分~~（已落地）· ~~`pipeline.py` 拆分~~（已落地）· 压测基线（**口径与脚本就位，数字待目标环境**）· ~~图写入后台化~~（已落地）· ~~真流式（TTFT）专项~~（已落地） |
| `02-能力建设与生成质量设计.md` | MP-04（**1 条**；`LM-04` / `MP-05` / `ST-01` 阶段 B 已于 2026-11-13 落地，见 §2.4 / §2.6 / §2.8 收尾说明） | `doc/plan/`（已撤销）整合（原上下文工程 + 生成质量 + 模型调用面 + 运维发布 + 多模态计划） | ~~上下文工程（分区器阶段 B）~~（已落地）· **模型调用面（VLM 结构化输出）** · **多模态尾项（live 复测 · 默认块大小）** |

> **两篇的分工**：第一篇是**规范线**（代码应该写成什么样 + 系统能扛多少），第二篇是**能力线**（系统应该长成什么样）。前者是后者的**前置**——没有异常基类与分层规则，能力改造会把同一份错误处理复制到多个新模块。

---

## 〇之二、本目录只列未实现项

按纪律 **D1**：已实现的能力 → `doc/analysis/`；本目录**不重复描述现状**——重复描述等于制造第二份真相源。

因此：

- 每篇 design 文档**不含**「已落地能力」的设计正文；
- 本索引**不含**已实现条目的行（状态为 ✅ / 🚫 的条目一律不在此表）；
- 想看「现在有什么」→ `doc/analysis/01-能力全景.md` 与 `doc/analysis/README.md` 关键数字表。

## 二、未实现项总表（跨计划汇总 · 2026-11-13 第三十五轮复核 · **1 条**）

> 这是本目录的**核心索引**。每条都指向具体设计文档的章节，**不重复写方案**。

### 2.1 正确性与安全（P0）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|

### 2.2 可度量性（P0/P1）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|

### 2.3 工程纪律（P0/P1）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|

### 2.4 架构与可观测性（P1/P2）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|
| MP-04 | **`--mode live` 复测从未真正执行**（仓内产物全是 `--mode local`，mock 向量） | `scripts/eval/run_eval.py`（live 分支可用但无产物）、`tests/data/eval/` | `02` §五之二 | 跑一次 live 并落盘；报告标注「基于真实链路」 |

> **2026-11-13（第三十一轮收尾）：`B0 X-27` 已落地并从本表移除。**
> `backend/app/services/pipeline.py` **1753 → 470 行**（验收 < 800 ✅）：第一步抽 `generation/{prompt,evidence}`（纯函数 + 回调注入，146 行）；第二步抽 `generation/{core,cache_stage,retrieve_stage,generate_stage,guard_stage,types}`（1569 行）——`core.py` 收编排与事件产出、`cache_stage` 收键与 single-flight、`retrieve_stage` 收预处理/过滤/证据装配、`generate_stage` 收 LLM 调用与降级/兜底/流式收尾/追问、`guard_stage` 收自检记录、`types` 收阶段间共享类型。**首参一律 `pip`**（`bind_principal` 的请求级副本），`pipeline.py` 保留同名字薄委托，子类与单测的调用面不变。**纯结构变更**：端点集合 / 响应结构 / 事件序列 / 缓存键 / 指标名不变；`tests/unit` **1010**（含补的 13 例阶段单测）· `tests/integration` 468 · `tests/format` 402 · `tests/e2e` 16 全绿，`ruff` 全绿，`A-14` 导入基线 492 不增。回归中修掉两个**拆分引入的真实缺陷**（阶段函数绕过 `pip._llm_generate` 替换点导致降级路径失去覆盖；两个「源码断言」测试在拆分后变成扫转发函数 → 假绿），详见 `doc/analysis/14` §3.0.27。收尾补 `tests/unit/test_generation_stages.py`（13 例）给拆出的阶段模块独立测试面；后端测试规模 133 文件 / 1907 例 → **134 / 1920**。
>
> **2026-11-13（第二十九轮收尾）：`A-12` 已落地并从本表移除。**
> `backend/app/main.py` **1282 → 54 行**：装配按「装配域」拆成 `bootstrap.py`（353 行：中间件 / 异常处理器 / 路由注册 / 启动期校验）+ `assembly/{context,runtime,models,storage,knowledge,pipeline,agent,hooks,lifespan}.py`（9 模块 1242 行）；探针独立成 `api/probes.py`（5 端点）+ `core/probes.py`（逻辑）。**纯结构变更**：拆分前后 `app.openapi()` 的路径+方法集合逐条一致（71 端点 / 64 路径），`tests/unit` + `tests/integration` + `tests/format` + `tests/e2e` 全绿，`ruff` 无新增告警。顺带修掉两处：「`main.py` 承担全部装配」这条结构债就此关闭；`SharedState` 双实例随之收敛到 `assembly/runtime.py:128` 唯一实例。登记见 `doc/analysis/14` §3.0.25，结构见 `doc/analysis/02` §1.1 / §二。
>
> **2026-11-13（第二十八轮收尾）：`A-26` 与 `MP-05` 已落地并从本表移除。**
> - `A-26`：`VECTOR_INDEX_TYPE`（默认 `hnsw`，`flat` 可回退）+ `VECTOR_INDEX_M` / `EF_CONSTRUCTION` / `EF_SEARCH`；FAISS `IndexHNSWFlat` + Milvus `add_index(index_type="HNSW")` + `search_params.ef`；**旧 FLAT 索引加载时就地重建**（>5 万向量只告警）。token 估算改为 CJK 感知（汉字/假名/韩文/全角 ≈1 token/字，拉丁 4 字符 ≈1 token），截断按样本密度反推字符预算。测试：`tests/unit/test_vector_index_type.py`、`tests/unit/test_context_assembly.py`。
> - `MP-05`：出厂默认 `800/100`（`core/config.py` / `services/chunking.py` / `chunkers/factory.py` / `.env.example` 四方一致）；`test_factory_default_is_800_100` 守住「四处不可漂移」。**真实绝对值仍待 `MP-04` live 复测**。
### 2.5 并发与容量（P1）

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|
> **2026-11-13（第三十五轮收尾）：`C-05` 已落地并从本表移除。**
> 判据不写成「序列数 < N」（会随业务增长误报），而写成两条可证伪的**基数性质**：① **不随请求量增长**——同形状流量打 5 倍后序列集合完全不变；② **取值有界**——`route` ∈ 路由模板 ∪ `{<unmatched>}`，任何 label 都不得出现 `tenant` / `user` / `doc_id` / `query` / `trace_id`。进程内判据 `tests/integration/test_metrics_cardinality.py`（5 例，CI 可闭环）+ 压测实例脚本 `scripts/metrics_cardinality.py`（每轮全新随机路径，`--snapshot-only` 出基数排行）。**写判据时发现并修掉一个真实缺陷**：未匹配路由（404 / 扫描器）的 `route` label 曾回退到 `request.url.path`，等于让任意请求方决定标签取值（`/wp-login.php`、随机 UUID 每次新建一条时间序列）→ 收敛到 `<unmatched>`；**日志**仍保留原始路径可检索，只有**指标**必须低基数。采样仍然**不做**（直方图本就按桶聚合，不驻留原始样本）。登记见 `doc/analysis/14` §3.0.32 与 `doc/analysis/并发容量基线.md`「指标标签基数（`C-05`）」节。

> **2026-11-13 收尾：`C-04` 与 `C-13` 已落地并从本表移除**（详见 `01` §10.3 / §10.6 的「已落地」小节与 `doc/analysis/14` §3.0.22）。
> - `C-04`（**2026-11-13 第二十八轮补齐残余**）：`_core_events` 现在真的在**阶段边界**检查 `cancel`（`preprocess` / `crag_round_N` / `retrieve` / `refine` / `generate`），取消时抛 `PipelineCancelled` → 半截结果不入缓存、不计成功率；`/ask`（非流式）新增 `_watch_disconnect` 主动探测，异常映射 499 + `4303 REQUEST_CANCELLED`（不计错误率）。测试：`tests/unit/test_cancel_stage_boundaries.py`（10 例）。
> - `C-13`：`ask_stream` 走 `_core_events(stream_tokens=True)`，生成阶段逐块 `yield "token"`（`services/pipeline.py:1143-1154`），首 token 不再等终稿；**方案 A**（禁重生成）——自检只下调置信度、不重写答案，`_final` 与 `confidence` 均带 `streamed_no_regen=True`；适配器无 `stream()` 时 `TypeError` 静默退回阻塞生成；流式路径**不做参数化兜底**、Agent 路径仍是后置切片（行为差异已登记 `analysis/04`）。**残余**：无 TTFT 实测数字、无自动化测试；`_finalize_streamed_answer(cancel=...)` 是未使用的死参数。

### 2.6 上下文与状态机（P0/P1）· 编号空间 `ST-`

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|
> **2026-11-13（第三十五轮收尾）：`ST-01` 阶段 B 已落地并从本表移除。**
> `services/context/render.py` 提供 `legacy` / `structured` 两种渲染（配置 `context_partition_render`，**默认仍是 `legacy`**）：`structured` 相对 `legacy` 只改**结构不改措辞**——五来源各自成段、记忆段带显式边界声明（「供参考，不必盲从」）、段序固定为「约束 → 记忆 → 历史 → 工具 → 证据」（证据离问题最近）；角色 / 注入防护 / 回答要求三段**原文复用** `generation/prompt.py`，避免把「提示词本身变了」混进对比。`PROMPT_TEMPLATE` 与 `MockLLM` 正则锚点**逐字节不变**（`tests/unit/test_context_render.py` 28 例，含 legacy 逐字门禁 4 例与锚点 2 例）。A/B 门禁 `scripts/eval/partition_ab.py` 分两级：超阈值**且显著**（配对 bootstrap）→ `regressions`；超阈值但**不显著** → `warnings`（仅留痕），`--from-report` 可换阈值零成本复算。60 题实测（`tests/data/eval/partition_ab.json`）：加权分 legacy **0.9428** → structured **0.9449**（Δ**+0.0021**，p=0.906 不显著），`conciseness` **−0.0243**（p=0.09 不显著）→ `regressions=[]`、`warnings` 1 条、`passed=true`。**因此不切默认**——切换应当是一次配置改动；judge 仍是 `heuristic` 且未做人工校准（缺 kappa / 一致率）。登记见 `doc/analysis/14` §3.0.31。

### 2.7 生成质量判定（P0/P1）· 编号空间 `EV-`

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|

### 2.8 模型调用面（P0/P1）· 编号空间 `LM-`

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|

> **2026-11-13（第二十八轮）：`LM-04` 已落地并从本表移除。**
> `parse_caption_sections` / `render_caption`：JSON（英/中键）与「标签行」双变体解析，命中 ≥2 段则规范化重排（主体 / 属性 / 场景 / 图中文字），否则原文兜底；缺段留空并 `logger.info` 记录。测试：`tests/unit/test_caption_structure.py`。

### 2.9 运维与发布（P0/P2）· 编号空间 `OP-`

| # | 未实现项 | 现状证据 | 设计文档 | 验收 |
|---|---|---|---|---|

> **另登记三项「口径与实现不符」的微缺口**（不占编号空间，修正动作见 `02` §五之二）：409 幂等冲突缺 `Retry-After` 头 · OTel 默认关闭 · `README.md:129` 的限流口径写「令牌桶 + 滑动窗口」而实现只有令牌桶。

---

## 三、推进顺序建议

| 梯队 | 条目 | 前置 |
|---|---|---|
| ~~第一梯队 · 工具地基~~ ✅ 已落地 | ~~`A-03` `backend/.gitignore` · `A-19` `gen_env_example.py` · `A-20` 写端点限流断言~~ | 2026-11-12 实施（现状见 `doc/analysis/14` §3.0.6） |
| ~~第二梯队 · 安全 / 数据正确性~~ ✅ 已落地 | ~~`A-04` 生产禁用 `/docs` · `A-05` `A-11` 探针回显与图存储损坏~~ | 2026-11-12 实施（现状见 `doc/analysis/14` §3.0.6） |
| ~~第三梯队 · 质量可度量~~ ✅ 已落地 | ~~`A-09` `A-10` 错误指标与死码裁决~~ | 2026-11-12 实施（现状见 `doc/analysis/14` §3.0.7） |
| 第四梯队 · 工程收敛 | ~~`A-06` `A-07` `A-08` `A-13` `A-15~A-18` `C-07` `C-10~C-12`~~ ✅ 已落地（2026-11-12） · ~~`A-12` `main.py` 拆分~~ ✅ 已落地（2026-11-13，第二十九轮） · `B0 X-27` 结构拆分 · ~~`C-09` 图写入后台化~~ ✅ 已落地（2026-11-13） | 前三梯队 |
| 第四梯队补充 · 规范与一致性 | ~~`A-21~A-26`~~ ✅ 已落地（`A-26` 于 2026-11-13 完成 FLAT→HNSW 与 CJK 感知 token 估算） | 第四梯队前半 |
| 第五梯队 · 增量能力 | `C-02~C-06` 压测与前端背压 · ~~`C-13` 真流式~~ ✅ 已落地（2026-11-13，见 §2.5 收尾说明） | 第四梯队 |

**能力线（`02` 篇）可与规范线并行，但内部有严格前置**：

| 顺序 | 条目 | 前置 |
|---|---|---|
| 第一梯队（并行） | ~~`OP-01` runbook + 备份~~ ✅ 已落地（2026-11-13）· ~~`LM-03` 多模态并发闸门~~ ✅ · ~~`ST-01` 阶段 A（只观测）~~ ✅ · ~~`KQ-27` 提示词注入第一层~~ ✅ · ~~`LM-04` VLM 结构化输出~~ ✅ 已落地（2026-11-13） | 无 |
| 第二梯队 | ~~`ST-03` 统一组装~~ ✅ → ~~`ST-02` 历史摘要~~ ✅ → ~~`ST-04` 输出预算~~ ✅ | `ST-01` 阶段 A |
| 第三梯队 | ~~`LM-01` 模型路由降级~~ ✅ → ~~`LM-02` 批处理~~ ✅ | `ST-04` 的输出预算口径 |
| 第四梯队 | ~~`OP-02` 灰度自动回滚~~ ✅ → ~~`OP-03` 部署层 canary~~ ✅ | `OP-02` 的判据与对照分组 |
| 生成质量链 | ~~`EV-02` rubric → `EV-03` judge → `EV-05` 生成归因 → `EV-06` 门禁扩展~~ ✅ · ~~`EV-01` 金标扩容~~ ✅ · ~~`EV-04` 显著性~~ ✅ | 独立并行 |
| 生产化尾项 | `MP-04` live 复测 → `MP-05` 默认块大小 → ~~`MP-06` 策略推荐表~~ ✅；~~`MP-01~MP-03` 前端三件~~ ✅ | `MP-05` 依赖 `MP-04` 与 `EV-01` |

> **两条线的交界**：`ST-01` 阶段 B（结构分区）**故意不进能力线的链条**——它改变答案提示词结构，而「答案有没有变好」需要 `EV-03`（judge）才能度量。**阶段 B 应当等 `EV-03` 落地后再启动**，否则又是一次不可度量的行为变更。

> **反模式警告**：不要先做 A-12~A-14（结构拆分）再做 A-07~A-09（错误收敛）——拆完再收敛，等于把同一个错误处理复制到多个新模块。**先收敛，再拆分。** 不要先做 `EV-06` 的生成质量 CI 门禁再做 `EV-02`/`EV-03`——门禁阈值会又变成硬编码。不要在 `EV-01` 扩容完成前改基线文件——20 条样本上的基线不可信。

---

## 四、门禁接线与待建路径白名单

| 改造 | 落点 | 门禁形态 | 失败后果 |
|---|---|---|---|
| `ST-01` 分区一致性 | `tests/unit/test_context_partition.py` | 单测 | 分区结果不可复现 → 红 |
| `ST-01` 阶段 A 门禁 | 同上 | 逐字节比对 | 提示词结构意外变化 → 红 |
| `ST-04` 输出上限 | `tests/unit/test_llm_max_tokens.py` | 单测 | 上限未下发 → 红 |
| `B0 KQ-27` 注入样本 | `tests/data/eval/` 注入样本集 + 断言 | 评测回归（**不是质量指标**） | 文档指令被执行 → 红 |
| `LM-01` 降级行为 | `tests/unit/test_llm_router.py` | 单测 | 鉴权错误被降级 / 失败被吞 → 红 |
| `LM-03` 许可和 | `backend/app/core/config.py` 启动校验 + `scripts/capacity.py` | 启动即失败 / 脚本退出码 1 | 许可和超限 → 拒绝启动 |
| `LM-04` 结构化解析 | `tests/unit/test_caption_schema.py` | 单测 | 向量内容漂移 → 红 |
| `EV-02` rubric 校验 | `backend/app/services/evaluation/rubric.py` | 单测 + 跑批启动即校验 | rubric 格式错 → 评测任务红 |
| `EV-03` judge | `scripts/eval/run_eval.py --mode live` | nightly（真 judge）+ CI（`MockJudge`） | **CI 永不使用真实模型**（无 Key 会红） |
| `EV-04` 显著性 | `backend/app/services/evaluation/significance.py` | 单测 | 显著性**只打标不阻断**（噪声大，易误伤） |
| `EV-06` 指标补全 | `.github/workflows/ci.yml` `retrieval-eval` job | 门禁阈值读 rubric 文件 | 阈值不再硬编码在 CI |
| `EV-01` 防回退 | `tests/unit/test_eval_metrics.py` | 条数断言 | 金标被缩回 20 条 → 红 |
| `OP-01` 告警覆盖 | `scripts/check_doc_refs.py` | 新增告警必须有处置卡 | 新增告警漏写处置卡 → 红 |
| `OP-02` 自动回滚 | `tests/unit/test_canary_controller.py` | 单测 | 默认关时产生副作用 → 红 |
| `OP-03` 清单 | `scripts/check_doc_refs.py` | 路径存在性 | 清单被删 → 红 |
| `A-09` 错误指标 | `tests/unit/test_error_kind_metrics.py` + `tests/integration/test_error_kind_metrics.py` | 单测（码段→类别映射）+ 集成测试（触发 ≥3 个 `kind`） | `kquery_errors_total` 未暴露 / `kind` 缺失 → 红 |
| `A-10` 死码不变量 | `tests/unit/test_error_code_liveness.py` | 遍历 `ErrCode`：要么有生产引用，要么有保留理由 | 出现「既没接线、也没登记」的码 → 红 |
| `A-13` import 边界 | `tests/unit/test_import_boundaries.py` | AST 断言 | `core/` 反向 import `services/` → 红 |
| `A-20` 写端点限流 | `tests/unit/test_write_endpoints_rate_limited.py` | 遍历写路由断言 | 新增写端点漏挂限流 → 红 |

**`scripts/check_doc_refs.py` 的 `PROPOSED_PATHS` 白名单**——**2026-11-12 复核：本篇声明的待建路径已全部登记入库**（`scripts/check_doc_refs.py:104-149`），无需再新增；下表用于**核对**，新增待建路径时照此补齐：

| 类别 | 路径 | 白名单落点 |
|---|---|---|
| 上下文包 | `backend/app/services/context/`（partition / budget / expand / history） | `:104` |
| 生成与多模态 | `backend/app/services/multimodal/schema.py` | `:114` |
| 路由与灰度 | `backend/app/services/llm_router.py`、`backend/app/services/canary.py` | `:115-116` |
| 评测 | `backend/app/services/evaluation/{rubric, judge, judge_prompt, citations, significance}.py` | `:109-113` |
| 脚本 | `scripts/canary_promote.py`、`backup.py`、`restore_drill.py`、`eval/cost_quality.py` | `:117-120` |
| 文档与数据 | `doc/runbook/`；`tests/data/eval/{rubric.v1.yaml, answers.jsonl, judge_baseline.json, judge_report.json, judge_cache.json, cost_quality.json}` | `:121-128` |
| 单元测试 | `tests/unit/{test_context_partition, test_context_history_compaction, test_agent_context_assembly, test_llm_max_tokens, test_llm_router, test_batch_indexing, test_multimodal_bulkhead, test_caption_schema, test_canary_controller, test_eval_rubric, test_eval_judge, test_eval_significance, test_eval_generation_attribution, test_eval_citations, test_prompt_injection_guard}.py` | `:129-143` |
| 集成测试 | `tests/integration/{test_context_breakdown_api, test_llm_routing_degraded, test_eval_judge_api, test_eval_gap_report}.py` | `:144-147` |
| 部署与工作流 | `.github/workflows/eval-nightly.yml`、`deploy/k8s/canary/` | `:148-149` |

> **机制说明**（`scripts/check_doc_refs.py`）：
> 1. **先判存在、后查白名单**（`:159`）——已存在的文件天然通过，不必加白名单；
> 2. 白名单按**前缀**匹配（`:162`，`path.startswith(p.rstrip("/"))`），目录类路径写一项即可覆盖其下全部文件；
> 3. 白名单只接受 `backend|frontend|tests|scripts|deploy|doc|.github` 开头的路径（`REF` 正则，`:17-21`）——**写 `services/evaluation/rubric.py` 这种省略 `backend/app/` 的写法不会被识别**，也就守不住，路径必须写全。

---

## 五、文档纪律

| # | 规则 |
|---|---|
| D1 | 本目录**只写未实现项与规范**。已实现的能力 → `doc/analysis/`；已下线的能力 → 直接从本目录移除并在本目录留一句「随 X 下线」的说明。重复描述现状等于制造第二份真相源。 |
| D2 | 每条未实现项必须给出：**现状证据（`文件:行号`）** · **目标态** · **落点文件** · **验收方式**。缺一不可。 |
| D3 | 设计文档**不钉代码基线 SHA**，一律写「见 `doc/analysis/README.md` §1.5」。 |
| D4 | 未实现项编号**沿用源计划**，不重新编号；跨计划汇总见 §二。 |
| D5 | 任何一条未实现项被实现后：① **从本目录移除该条**（其现状并入 `doc/analysis/` 对应篇）；② `doc/analysis/14-差距与演进.md` 的缺口台账移入「已修」；③ 本目录 §二 总表同步。**三处必须同步。** |
| D6 | 禁止绝对路径（`/Users/`、`C:\`）；路径相对仓库根。 |
| D7 | 本目录**不含工期、人力与排期**——本目录只回答「**长什么样**」与「**该守什么规范**」。 |

---

## 维护触发

- 本目录必须更新的条件（触发任一即需复核）：
  - `doc/analysis/14-差距与演进.md` 的缺口台账变化
  - `backend/app/core/envelope.py`（错误码枚举）、`backend/app/domain/schemas.py`（领域模型）
  - `backend/app/services/pipeline.py`、`backend/app/services/pipeline_executor.py`、`backend/app/services/upstream.py`（并发与降级）
  - `backend/app/main.py` / `backend/app/bootstrap.py` / `backend/app/assembly/*`（装配结构；装配顺序的唯一真相源是 `assembly/lifespan.py`）
- **2026-10-30**：本目录建立，汇总当时 `doc/plan/` 四篇计划 + 缺口复评清单的全部机械可验未实现项共 **48 条**。
- **2026-11-12（上午）**：① 认证与权限整套下线，本目录移除全部认证 / 权限相关项；② 移除已实现条目的设计正文（其中唯一未实现的 `B0 KQ-27` 提示词注入面迁入能力篇）；③ `A-01`（ruff 规则集）/ `A-02`（CI 静态护栏）/ `X-25`（CI 四个 job）/ `X-13`（top_queries 淘汰）/ `B0 KQ-09`（检索金标）/ `B0 KQ-16`（硬截断可达）/ `B0 KQ-26`（敏感词，已下线）/ `C-01`（多租户压测，随认证失效）经代码复核确认已实现或已失效，从本目录移除。
- **2026-11-12（下午）**：本目录由 6 篇整合为 **2 篇**——`01-工程基线与质量保障设计`（规范线：工程规范基线 + 错误体系 + 结构 + 并发）与 `02-能力建设与生成质量设计`（能力线：上下文工程 + 生成质量 + 模型调用面 + 运维发布）。原 03/04/05/06/07 五篇随之删除；两篇均**不含代码与伪代码**，改为「现状证据 → 目标态 → 落点文件 → 验收」的四段式描述，并新增**项目文件层次规范**与**语言与命名规范**两章。
- **2026-11-12（晚）**：**`doc/plan/` 目录撤销**——十篇计划中「仍未实现的计划」与「代码规范」并入本目录（新增 `A-21 ~ A-26` 死代码 / 失真注释 / 一致性缺口、`MP-01 ~ MP-06` 多模态与生产化尾项，以及「文档写作规范」与「规范可量化基线」两节）；「已完成的实施记录」与「敏感词 / 权限 / 配额 / 审计相关内容」按纪律删除。§二 总表由 48 条（另迁入 `B0 KQ-27` 计 49 条）增至 **61 条**。
- **2026-11-12（晚·核对）**：对两篇与本索引的**全部 `文件:行号` 证据与计量数字**做了一轮代码复核（AST + grep 实测），共修正 **28 处**（`01` 附录一 10 处 · `02` §七之一 8 处 · 本索引 10 处）并补 3 处口径澄清——详见 `01` 附录一与 `02` §七之一。要点：① `C-07` 原写「`kquery_upstream_in_use` 全仓无写入点」**是错的**（`main.py:645-657` 每次抓取都回填）；② `EV-02` 原写「`rubric` 仅命中 ACL 变量」**是错的**（全仓 0 命中）；③ `02` 原写 `services/metrics_query.py`「已装配接线」**是错的**（只有一条 import、0 使用点）；④ 多处行号漂移（`pipeline.py:66`、`graph.py:721` / `:736-737` / `:552`、`core/config.py:260-268` / `:368`、`ingest.py:82`、`filters.py:14`、`main.py:906`）；⑤ `core/` 并无 `ratelimit.py` / `upstream.py`（实现都在 `services/`）；⑥ `PROPOSED_PATHS` 白名单**早已补齐**，旧稿「需要新增」的口径作废。
- **2026-11-14（实施·三十八）**：**第四个 provider（智谱）接入 + 仓库整理后全量复测**——① **能力实测**（真 key，`open.bigmodel.cn/api/paas/v4`）：`/chat/completions`（`glm-4.6`/`glm-4.5`/`glm-4.5-flash`/`glm-4-air`/`glm-4-flash`）· `/embeddings`（`embedding-3`，**2048 维**，L2=1.0000）· `/rerank`（`model=rerank`，`relevance_score`）**三通道齐全**——补上「只有 MiniMax 有向量」「只有对话模型能做重排」两个孤岛。② **唯一坑从隐式变显式**：`glm-4.5` 系**默认开思考**，思考 token 计入 `max_tokens`，预算小时 `finish_reason=length` 且**正文为空且不报错** ⇒ `ZhipuLLM` 默认下发 `thinking={"type":"disabled"}`（`ZHIPU_THINKING_ENABLED` 可开）。③ **接入复用优先**：`ZhipuLLM(DeepSeekLLM)` 只覆盖新加的基类钩子 `_extra_payload()`；向量/重排直接复用 `OpenAICompatibleEmbeddings` / `OpenAICompatibleReranker`（智谱是标准 OpenAI 形状，不需要像 `MiniMaxEmbeddings` 那样单独写）。④ **配置面**：`EMBEDDING_PROVIDER_DEFAULTS["zhipu"]={embedding-3,2048}`、`PROVIDER_MODEL_DEFAULTS["zhipu"]` 四档、`auto` 链 **deepseek → minimax → zhipu → dashscope → mock**、维度守卫（`embedding-3` + 出厂 `dim=1024` → 2048）、重排名守卫（`gte-*` → `rerank`）、**OCR 回落 dashscope**（`glm-4.5v` 是对话式多模态，非 `/ocr` 协议）。⑤ **混合链路真跑**：MiniMax `embo-01` 1536 维真向量入库（Milvus Lite）→ 检索 → **DeepSeek** listwise 重排 → **智谱 `glm-4.5-flash`** 生成，答案带引用、`score=1.0`。⑥ **复测与整理**：新增 `tests/unit/test_zhipu_provider.py`（19 例），后端 **149 文件 / 2051 例**（unit **104/1135** · integration 33/473 · format 5/389 · e2e 2/16 · e2e_ui 3/18 · contract 1/10 · security 1/10），七层整跑 **2017 passed / 34 skipped / failures=0**；`backend/.env.example` 改为**跑生成器重建**（手改会被 `test_env_example.py` 判红）；`ruff check` 全绿。**未做全量 `ruff format`**：会重排 300+ 文件并让文档里成百上千处 `文件:行号` 证据引用一起失效，收益小于可追溯性损失。登记见 `doc/analysis/14` §3.0.35。
- **2026-11-14（实施·三十七）**：**语音能力整体下线 + 真实向量/重排通道打通**——① **语音下线**（六层一致）：删 `services/speech/`（ASR + STS 双通路）、`services/parsers/audio.py`、`api/speech.py` 三端点、`config.py` 的 `transcribe_model` / `audio_*` / `volc_*` / `speech_*` 共 14 字段与 `resolved_audio_provider`、错误码 `3802` / `3803`、前端 `VoiceRecorder.tsx` + `ChatInput` 的 `voice` 插槽 + STS/转写 API 客户端 + 上传向导「转写」开关、7 个测试文件与 8 处 conftest/mock 引用、`07` 两章与 `03` 接口表等文档。连带：router 11 → 10、端点 72 → 69、扩展名 53 → 50（格式族 10 → 9、解析器 8 → 7）、`api/` 13 → 12 文件。② **「用 DeepSeek 的 key 做 embedding/rerank」实测不可行**（供应商边界，不是配置问题）：真实 key 打 `GET /v1/models` 200（只有 `deepseek-flash` / `deepseek-v4-pro` 两个对话模型），`/v1/embeddings`、`/beta/embeddings`、`/v1/rerank` **全 404**；同时查出 `.env` 里的百炼 key **账号欠费**（`400 Arrearage`，`text-embedding-v3` / `gte-rerank-v2` 都调不通），以及代码里「MiniMax 不提供 embeddings」的断言**是错的**（`embo-01` 实测可用，1536 维、已 L2 归一化、请求体 `texts`/`type`，非 OpenAI 形状）。③ **落地通道**：新增 `MiniMaxEmbeddings`（`EMBEDDING_PROVIDER=minimax`，业务错误藏在 HTTP 200 的 `base_resp.status_code` 里、条数/维度不符在入库前拒绝、`db`/`query` 两种投影不混用）+ `LLMReranker`（`RERANK_PROVIDER=deepseek`，**对话模型 listwise 重排**走 `/chat/completions`，容错解析 + 漏写候选补尾 + 单调递减伪分数 + 失败抛 `RerankError` 降级为融合序）；`EMBEDDING_PROVIDER_DEFAULTS` 与新增的 `resolved_embedding_model/dim/base_url`、`resolved_rerank_model/base_url` 让「换 provider 忘改模型名/维度」不再靠人记（`embo-01` 强制 1536，千问重排名 `gte-*` 自动换成对话模型）；`config_store` 热配置默认值改用 `resolved_rerank_model`。④ **补掉一处真空缺**：向量库**此前完全不校验维度**（FAISS 读旧索引 / Milvus 复用旧集合都照单全收，1024 → 1536 后第一次检索或入库才炸且报错看不出是配置）——现在 FAISS 维度不符 `StorageCorruptedError` 拒绝启动、Milvus 维度不符 `drop + 重建` 并 WARNING。测试：新增 `test_minimax_embeddings.py`（9）+ `test_llm_reranker.py`（15）+ `test_vector_rerank_live.py`（5，默认 skip），后端 **148 文件 / 2032 例**（unit 103/1116 · integration 33/473 · format 5/389 · e2e 2/16 · e2e_ui 3/18 · contract 1/10 · security 1/10），unit+integration+format+contract+security 实跑 **1982 passed / 16 skipped**；live 5 条真 key 全通过，**端到端真跑**：上传 md → 1536 维真向量入库（Milvus Lite）→ 检索命中 → DeepSeek 重排（相关块 `rerank_score=1.0`）→ 生成答案正确并带引用。**§二 总表不变（仍 1 条 `MP-04`）**：真实向量通道虽已打通，但评测集仍在 mock 口径上产出，`MP-04` 要的是「真实向量上重跑评测」，本轮没做。登记见 `doc/analysis/14` §3.0.34（并给 §3.0.33 的「MiniMax 无 embeddings」加了前向更正）。
- **2026-11-14（实施·三十六）**：**MiniMax 接入（第三个生成 provider）**——`MODEL_PROVIDER=minimax` 可用，**传输层零改动**（复用 `services/openai_compat.py`，Deadline + bulkhead 一并继承），新增 `MiniMaxLLM`（`DeepSeekLLM` 子类，只覆盖差异点）。本轮价值在两处**非标准行为从隐式变显式**：① **业务错误藏在 HTTP 200 的 `base_resp.status_code`** 里（`1001` 超时 / `1002` 限流 / `1004` 鉴权 / `1008` 余额 → 领域错误码 + `retryable`/`degradable`，且**业务错误不重试**）——不检查就会把空 delta 当「模型没话说」；② **推理模型恒开思考**：`reasoning_content` 与正文共享 `max_tokens`（`MiniMax-M3.1-Flash` 上限小 → `finish_reason=length` 且正文为空），故流式**跳过思考段**、`_output_budget()` 加 1024 余量、`_assert_payload()` 把空正文判成 `GENERATION_FAILED`（不可重试但可降级，交给路由）。配套：`PROVIDER_MODEL_DEFAULTS` 按 provider 替换千问模型名（否则首请求 404 且看着像 key 不对，替换在 `provider_notes()` 留痕）；`CLOUD_ONLY_GENERATION_PROVIDERS={deepseek,minimax}` 让 embeddings / rerank / OCR / ASR 四通道回落 dashscope（新增 `resolved_audio_provider`）；`build_vlm_answerer` 按 provider 选实现（千问 `file://` / MiniMax base64 `data:` URL）。测试：`test_minimax_provider.py` 24 例 + `test_minimax_live.py` 6 例（默认 skip，真实 key 本机 6/6 通过，含视觉直答读边框颜色）；后端 **148 文件 / 2071 例**（unit 103/1125 · integration 33/485 · format 5/407 · e2e 2/16 · e2e_ui 3/18 · contract 1/10 · security 1/10），unit+integration+contract+security 实跑 **1623 passed / 7 skipped**。**§二 总表不变（仍 1 条 `MP-04`）**：MiniMax 不提供 embeddings，`MP-04` 要的是真实向量链路的绝对召回，与本轮无关。登记见 `doc/analysis/14` §3.0.33。
- **2026-11-13（深夜·实施·三十五）**：**最后两条设计项落地**（`ST-01` 阶段 B · `C-05` 判据闭环）——① **`ST-01` 阶段 B 结构分区渲染**：`services/context/render.py` 的 `legacy` / `structured` 双模式（配置 `context_partition_render`，**默认仍是 `legacy`**）；`structured` **只改结构不改措辞**（五来源分段 · 记忆段显式边界 · 段序「约束→记忆→历史→工具→证据」），角色/注入防护/回答要求三段原文复用 `generation/prompt.py`；`PROMPT_TEMPLATE` 与 `MockLLM` 正则锚点**逐字节不变**；A/B 门禁 `scripts/eval/partition_ab.py` 改为**两级**（超阈值且显著 → `regressions`；超阈值不显著 → `warnings` 仅留痕），并新增 `--from-report`（换阈值零成本复算）；60 题实测加权 Δ**+0.0021**（p=0.906）、`conciseness` −0.0243（p=0.09）→ 判「未显著劣化」，**不切默认**。② **`C-05` 标签基数判据闭环**：判据从「序列数 < N」改成两条**可证伪性质**——不随请求量增长（5 倍流量后序列集合不变）+ 取值有界（`route` ∈ 路由模板 ∪ `<unmatched>`，禁止 `tenant`/`user`/`doc_id`/`query`/`trace_id` label），进程内判据 `tests/integration/test_metrics_cardinality.py`（5 例）+ 压测脚本 `scripts/metrics_cardinality.py`。**写判据时发现并修掉一个真缺陷**：未匹配路由的 `route` label 曾回退到 `request.url.path`（任意请求方决定标签取值 → 无界时间序列），收敛到 `<unmatched>`。按纪律 D5 移除这两条：**§二 总表 3 条 → 1 条**（`01` 篇 1 → 0 条，`02` 篇 2 → 1 条）；仅剩 `MP-04`（需真实上游凭证）。登记见 `doc/analysis/14` §3.0.31 / §3.0.32。
- **2026-11-13（深夜·实施·三十四）**：**测试类型补全 + SSE 可恢复 + 缓存 ANN**（4 项，均为上轮登记的「仍未闭合」）——① **`tests/contract/`**（`X-30`）：从 `app.openapi()` 枚举 **72** 个操作，断言路由真实挂载（404 不能是「路由未命中」）、错误码落在 `ErrCode` 值域、404 分层（1004 vs 3xxx）、405 信封化、`X-Trace-Id` 头/信封一致、业务端点声明 `Envelope`、**26** 个带路径参数操作全部登记归属校验（10 例）② **`tests/security/`**（`X-30`）：固定身份下没有第二个角色可伪造，故矩阵钉两条**真实**路径——自报身份头无效 + 按 id 猜别人的会话/文档/快问/HITL run/成本账单/反馈工单/查询分析记录（10 例，404 且无副作用）③ **SSE 断点续传**（`X-21` 残余）：帧缓冲（TTL 120s / 帧上限 / 溢出后帧不再带 `id`）+ 生成泵与连接解耦 + 断线宽限期（20s）+ `GET /ask/stream/{trace_id}`（`Last-Event-ID: {trace_id}:{seq}`）+ `X-SSE-Resume: enabled` 能力头 + 4304；前端**只认能力头**才续传（2 次，300ms/900ms 退避），**默认关闭**（关闭时零额外内存、行为与旧版一致）④ **语义缓存 LSH**（`KQ-17` 残余）：12 bit 随机超平面 + 汉明 ≤2 邻桶只筛候选，命中判定与三道闸**一字未改**；<512 条自动回退全扫；逐出同步出桶。⑤ 顺带修掉 **`X-22`** 两个残余：retry 连同失败气泡与原始提问一起删（不再重复问答）、轮次快照落 `trace_id`（恢复历史后成本/日志入口仍可用）。**新用例一写就红的 4 个真缺陷**：① 6 个 API 模块 **60 个 handler** 各自 `new_trace_id()` 覆盖中间件注入值 → 头与 `meta.trace_id` 不一致（改用 `get_trace_id()`）② 反馈工单 / 查询分析记录只按 id 取，未断言租户（补 `tenant_id/user_id` 落库与过滤）③ `GET /ask/stream/{trace_id}` 受限流守卫拦下——**登记例外而非去掉限流**（每次附着都起尾随任务 + 读缓冲，按写流量计合理）④ `.env.example` 落后生成器（差 550 行）。测试：后端 **144 文件 / 2008 例**（unit 101/1073 · integration 31/474 · format 5/407 · e2e 2/16 · e2e_ui 3/18 · contract 1/10 · security 1/10）实跑全绿；前端 **18 文件 / 113 例** + `tsc --noEmit` + `build` 全绿。登记见 `doc/analysis/14` §3.0.30。
- **2026-11-13（深夜·实施·三十一）**：**`B0 X-27` 第二步落地，条目关闭**（纯结构变更）——`services/pipeline.py` **1606 → 470 行**（验收 < 800 ✅，原始 1753）。新拆 6 个模块入 `services/generation/`：`core.py`（779：`core_events` 编排 / `ask` / `ask_stream` / `assemble` / `record` / `record_quality` / `persist_round` / `log_stage_summary` / `cached_result` / `cached_stream_events`）、`cache_stage.py`（159：键 / 语义近似读写 / single-flight）、`retrieve_stage.py`（172：`degrade_level` / `preprocess` / `build_filters` / `to_evidence` / `build_context_breakdown` / `observe_retrieval`）、`generate_stage.py`（340：LLM 调用 / 降级 / 兜底 / 流式收尾 / 追问）、`guard_stage.py`（46：自检记录与落库）、`types.py`（73：`PipelineCancelled` + 路由判定 + 上游可用性判定）。**首参一律 `pip`**（`bind_principal` 的请求级副本，避免把单例状态塞进协作者）；`pipeline.py` 保留同名字薄委托（`_core_events` / `_cache_key` / `_to_evidence` / `_llm_generate` / `_finalize_streamed_answer` / `_to_self_check_record` …），子类 override 与单测替换点不变；包内改用相对导入，`A-14` 基线 **492 → 492**。**回归中修掉两个拆分引入的真缺陷**：① 阶段函数一度直接调本模块 `llm_generate()`，绕过 `pip._llm_generate` 这个既有替换点 → 上游故障不再降级（`test_degrade_generate_template.py` 4 例红），改为回打 `pip._*` 方法；② 两个「源码断言」测试（取消检查点 / 闲聊分支不声明 `reranked`）在拆分后扫到转发函数 → 断言空转（假绿），改为扫 `generation.core.core_events`。测试：`tests/unit` **1010**（收尾补 `tests/unit/test_generation_stages.py` 13 例，给阶段模块独立测试面）· `tests/integration` 468 · `tests/format` 402 · `tests/e2e` 16 全绿，`ruff` 全绿，后端规模 134 文件 / 1920 例。按纪律 D5 从 §二 移除该条：**总表 3 条 → 2 条**（`01` 篇 2 → 1 条）。登记见 `doc/analysis/14` §3.0.27。
- **2026-11-13（夜·实施·三十）**：**`B0 X-27` 第一步落地**（抽纯函数，纯结构变更）——`services/pipeline.py` **1753 → 1606 行**；新包 `services/generation/`：`prompt.py`（86 行：`PROMPT_TEMPLATE` / `GENERAL_KNOWLEDGE_PROMPT` / `REASON_*` / `EVIDENCE_BLOCK_*` / `build_evidence_block` / `build_prompt`）、`evidence.py`（163 行：`asset_uri` / `avg_score` / `build_candidates`（父块展开+去重）/ `evidence_for_assembled` / `rejected_evidence`）。**依赖一律用参数传入**（`name_of` / `parent_of` 是回调而非 pipeline 引用），`_to_evidence` 退化为「读设置 → 调纯函数 → 收集观测值」21 行。`pipeline.py` 顶部保留同名再导出（`build_evidence_block` / `PROMPT_TEMPLATE` / `GENERAL_KNOWLEDGE_PROMPT` / `_asset_uri` …），调用方零改动。新增 `tests/unit/test_generation_parts.py`（24 例：边界标记成对性 / 跨文档分隔 / 父块去重 / rerank 降级回退 / 采纳与未采纳证据 rank 连续 / 图像资源名 / 再导出同一性）。回归：`tests/unit` 997 passed · `tests/integration` 468 passed / 1 skipped · `tests/format` 402 passed / 5 skipped · `tests/e2e` 16 passed · `ruff` 全绿。`A-14` 基线 484 → **492**（新增导入全是「生成层 → core / domain / 同层服务」的正确方向）。**第二步未做**：缓存段、流式收尾、自检/CRAG、`_core_events`（415 行）仍在该类内。
- **2026-11-13（夜·实施·二十九）**：**`A-12` 装配层拆分落地**（纯结构变更，无行为变化）——`backend/app/main.py` **1282 → 54 行**，只保留「建应用 + 兼容导出」；拆出 `bootstrap.py`（353 行：CORS / trace 中间件 / 五档异常处理器 / 路由注册 / 启动期配置校验）、`assembly/` 9 模块 1242 行（`context` 装配聚合 / `runtime` 进程参数+指标+共享状态+缓存+限流 / `models` / `storage` / `knowledge` / `pipeline` / `agent` + 工具池 / `hooks` / `lifespan` 装配顺序与关停）、探针独立成 `api/probes.py`（5 端点）+ `core/probes.py`（逻辑）。**验收方式**：拆分前后 `app.openapi()` 的「路径+方法」集合逐条比对为空 diff（71 端点 / 64 路径）；`tests/unit` / `tests/integration` / `tests/format`（402 passed / 5 skipped）/ `tests/e2e`（16 passed）全绿；`ruff check backend/app tests` 无告警。**顺带修掉**：`SharedState` 双实例收敛为 `assembly/runtime.py:128` 唯一实例；`_record_error` 不再从 `main.py` re-export（否则打补丁会打到无效副本，见 `tests/unit/test_cancel_stage_boundaries.py:204` 注释）；`A-14` 基线 449 → **484** 并写明增量全部来自「装配层依赖 services/core」的正确方向。按纪律 D5 从 §二 移除该条：**总表 4 条 → 3 条**（`01` 篇 3 → 2 条）。登记见 `doc/analysis/14` §3.0.25，结构见 `doc/analysis/02` §1.1 / §二。结构债转移：`services/pipeline.py` **1753 行**成为唯一剩余上帝对象（`B0 X-27`）。
- **2026-11-13（深夜·实施·二十八）**：**未实现项落地**（5 条 + 3 条残余关闭）——`LM-04` VLM caption 结构化解析（JSON/标签行双变体 + 原文兜底）· `MP-05` 切块出厂默认 300/50 → **800/100**（四处字面量一致，`EV-04`/`EV-06` 证据支撑）· `A-26` 向量索引 **FLAT → HNSW**（可配 `M`/`efConstruction`/`efSearch`，旧 FLAT 索引加载时就地重建）+ token 估算改 **CJK 感知** · `C-09` 图写入**后台任务化**（单 worker 有界队列 + `GET /graph/build/{task_id}` 进度查询，移出后台闸门名额）· `C-04` **阶段边界检查**与**阻塞路径断开探测**补齐（取消 → 499 + `4303 REQUEST_CANCELLED`，半截结果不入缓存）。按纪律 D5 从 §二 移除这 5 条：**总表 9 条 → 4 条**（`01` 篇 5 → 3 条，`02` 篇 4 → 2 条）；残余缺口 `N-11` / `N-12` 关闭，`SharedState` 双实例与 `/metrics` fail-closed 死分支代码已修。剩余 4 条各有**不做**的明确理由：`A-12` / `B0 X-27`（纯重构需独立回归窗口）· `MP-04`（需真实上游凭证）· `C-05`（需独立压测环境）· `ST-01` 阶段 B（仓库纪律要求先人工校准 `EV-03` judge）。测试侧顺带修掉 3 个「测试从未真跑」的掩盖：`pymilvus` import 时 `load_dotenv()` 把 `backend/.env` 注入 `os.environ`（`CHUNK_SIZE=300` 由此绕过 conftest）· `asyncio.run` 与 `pytest-playwright` running loop 冲突 · 端点守卫数字未随新增端点同步。登记见 `doc/analysis/14` §3.0.24。
- **2026-11-13（夜·复核·十七）**：**全目录逐篇回扫**（不改代码，只对齐文档）——`01` / `03` / `05`～`11` / `13` 九篇正文与 `doc/runbook/` 仍带 2026-10-28 口径（认证 / ACL / 知识库实体 / 配额 / 敏感词 / 审计仍在世），本轮逐篇按「已下线（2026-11-12）」改写，并修掉 `05` 的 `acl_version` 缓存维度、`06` 的审计留痕与 PII 硬拦截、`10` 的 `healthz` auth 探针。**未实现清单不变（9 条）**：`A-12` / `B0 X-27` / `A-26` / `MP-04` / `MP-05` / `C-05` / `C-09` / `ST-01` 阶段 B / `LM-04`。同时发现 4 条代码缺陷（`doc_facts.py` 的 `KeyError: 'path'` 已修；`/metrics` fail-closed 死分支、`_finalize_streamed_answer` 死参数、闲聊 `reranked` 硬编码待修），登记于 `doc/analysis/14` §3.0.23。
- **2026-11-13（傍晚·实施·十六）**：**真流式与断开取消落地**（2 条）——① `C-04` 客户端断开主动取消：`_watch_disconnect()` 0.5s 轮询 `is_disconnected()` + 生成循环每 token 检查 `cancel`（**残余**：阶段边界与阻塞路径未覆盖）② `C-13` 真流式（TTFT）：`ask_stream` 逐块外发 `token`，**方案 A 禁重生成**，自检只下调置信度并标记 `streamed_no_regen`。按纪律 D5 从 §二 移除这 2 条：**总表由 11 条 → 9 条**（`01` 篇 7 条 → 5 条）。两项均**无自动化测试**、`pipeline.py` 由 1507 → **1673 行**（拆分难度上升），故同步登记残余缺口到 `doc/analysis/14` §3.0.22。
- **2026-11-13（午后·审计）**：**全量代码审计 + 文档与代码对齐**（不新增功能）——① 逐条核对剩余 **11 条**设计项的真实状态（**8 条完全未实现**、3 条部分实现），**§二 总表的「现状 / 证据 / 验收」三列按代码复核结果重写**；② 复核前 24 轮实施登记（§3.0.10 ~ §3.0.20）的锚点：**无虚假声明**，只有 2 处路径 / 命名口径偏差 + 1 处过期陈述；③ **把 `doc/analysis/01` ~ `13` 与根 README 的 30+ 处过时陈述改回与代码一致**（最严重的是「不存在检索金标集 / recall@k 无从测量」——已落地 120 条金标 + 全套指标；另有 HITL 前端消费方、注入面、router 数 / 端点数 / 测试数 / 脚本数 / 错误码数等一批数字过期）；④ 修正 `01` 篇 **§10.3 `C-04` 的失真陈述**（「管线也有阶段边界检查」→ 实测 `pipeline.py` 内 `cancel` 零命中，缺的是**两件**不是一件）与 §10.1 现状速览；⑤ §3 缺口台账中 `KQ-27` / `X-12` / `KQ-09` **三条闭合**就地标注。审计记录见 `doc/analysis/14` §3.0.21。
- **2026-11-13（午·实施·十五）**：**实验可见 + 切块可配 + 策略表**（3 条）——① **`MP-02` 配置页实验分组视图**（列出作用域 / 覆盖口径 / 实验标签 / 生效状态 + 优先级说明；**作用域读不到不让整页失败**）② **`MP-06` 按文档类型的检索策略推荐表**（`doc/analysis/检索策略推荐表.md`：六类文档 × 策略 / 块大小 / 检索路偏好，**每条写明依据** + 分流时的三个坑）③ **`MP-05` 切块策略配置化**（预设表搬到 **core 层**——它是配置数据，生产与评测共用一份；`chunk_policy` 配置项 + `resolved_chunk_policy`；**出厂默认仍是 300/50**，改法 = 一行配置，但需 `MP-04` live 复测后由人决策）。按纪律 D5 从本目录移除 `MP-02` / `MP-06`；§二 总表由 **13 条 → 11 条**，`02` 篇 7 条 → 4 条。
- **2026-11-13（午·实施·十四）**：**发布可控 + 工具可用性可见**（2 条）——① **`OP-03` 部署层 canary**（`deploy/k8s/canary/`：同镜像同配置的 canary Deployment + **专属 Service**（标签多一个 `track: canary`——标签相同会被 base 的 Service 选中，canary 与稳定版共享流量，canary 就失去意义）+ 注解式 Ingress；`scripts/canary_promote.py` **复用 `OP-02` 的控制器与口径**（判定标准漂移一次就会推出互相矛盾的结论），**默认 dry-run**，**无指标 → 不判定**；代价写在 README：权重粒度粗（副本数比例）、但**回滚秒级**）② **`MP-01` 工具失败率卡片**（此前只有「调用分布」，**分布看不出失败**；失败率是工具可用性的**先行指标**——工具大面积失败时答案质量还没掉，但已不该继续用这条链路；复用既有工具调用记录，**不新增指标名**）按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.19；§二 总表由 **15 条 → 13 条**，`02` 篇 9 条 → 7 条。
- **2026-11-13（午·实施·十三）**：**补上流程的闭环**（3 条）——① **`OP-02` 灰度自动回滚**（`services/canary.py`：规则 + 判定 + 控制器；**无基线只告警不动作**——不能拿「无数据」当「劣化」；样本下限；回滚动作是 `clear_scoped`（幂等、语义干净）；**多副本必须配 Prometheus**，未配置时返回 None 不猜数；**默认关**）② **`LM-02` 批处理通道**（图像索引批量：**逐张成败独立、部分失败不回滚整批**；`/media/index` 支持多张；`BatchJudge`：**批量不改变单条语义**，单条失败不影响整批；**token 记账仍逐条累加**）③ **`MP-03` 前端 409 语义**（409 = 「同一问题正在处理中」，**不是故障**；与背压**同源**的 helper；**500 仍不带 `retry_after`**）按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.18；§二 总表由 **18 条 → 15 条**，`02` 篇 11 条 → 9 条。
- **2026-11-13（午·实施·十二）**：**长期成本可见化**（4 条）——① **`ST-02` 历史摘要压缩**（四段结构化摘要；**预算闸门前置**：预算不够直接截断回落，不为了省 token 先烧预算；模型不可用不抛错；摘要用纯文本不用 JSON；`sessions.py` 那句失实的「摘要压缩默认关闭」改成如实描述）② **`C-02` / `C-03` 压测基线留档**（`doc/analysis/并发容量基线.md` 追加式基线表 + `load_test.py --markdown-row` 直接产出基线行 + 列数一致性守卫；**503 不计入 5xx**；**v2 数字留空并写明为什么**：开发环境的数字不可外推）③ **`EV-06` 指标补全 + 成本-质量曲线 + 生成质量入 CI**（`precision_at_k` 分母用 min(k, 实际候选数)· `hit_rate_at_k`；帕累托前沿；CI 新增成本-质量与**启发式判定**门禁——**CI 绝不调真模型**）④ 顺带得出**第二个有统计依据的结论**：`recursive-800-100` 在每个维度上不劣于默认且**便宜 31%**（帕累托前沿只含它一套，与 `EV-04` 的显著性结论方向一致；**默认仍未改**）。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.17；§二 总表由 **22 条 → 18 条**，`01` 篇 9 条 → 7 条，`02` 篇 13 条 → 11 条。
- **2026-11-13（午·实施·十一）**：**上下文统一 + 输出预算 + 显著性**（3 条）——① **`ST-03` Agent 路径接入统一上下文组装**（父块展开抽到 `services/context/expand.py`，**去重键仍是父块 ID**；Agent 证据此前**无上限**，现在受 `context_token_budget` 约束；被裁 / 被去重的条目 → `dropped_evidence` → `context_breakdown.dropped`）② **`ST-04` 输出侧 token 预算**（`budget.max_output_tokens(reserve)` + `llm.generate/stream` 的可选 `max_tokens`，**None 时不下发**；预算耗尽 → **不发起调用**；配置 `llm_max_output_tokens` 兜底）③ **`EV-04` 配对 bootstrap 显著性**（纯 Python、零新依赖、固定 seed 可复现；**样本量 < 30 不判显著**；切块策略实验与基线对比都接上了）。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.16；§二 总表由 **25 条 → 22 条**。
- **2026-11-13（午·实施·十）**：**生成质量判定闭环**（3 条）——① **`EV-02` 版本化 rubric**（五维 v1；**加载即校验**：权重和必须为 1、关键维度必须是子集、缺字段立刻抛错——不允许「缺维度 = 0 分」的静默降级；文件用 **JSON** 而不是设计稿的 YAML，避免给项目新增 `pyyaml` 依赖）② **`EV-03` LLM-as-Judge**（默认 3 票 → 逐维度中位数；一致性取**各维度最小值**；**失败绝不伪造分数**；维度缺失 → 该票作废；**CI 替身 `HeuristicJudge`** 标清「非模型判定」；`run_eval.py` 新增 `--rubric` / `--judge`，live 模式采集答案，产出带 rubric 版本的 `judge_report.json`）③ **`EV-05` 生成侧归因**（引用抽取 + 支持性判定 + 五桶 `not_cited`/`misquoted`/`answered_wrong`/`correct`/**`unjudged`** + 检索×生成的归因矩阵）。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.15；§二 总表由 **28 条 → 25 条**，`02` 篇 19 条 → 16 条。
- **2026-11-13（午·实施·九）**：**生成质量前置 + 多模态治理**（2 条）——① **`EV-01` 金标集扩容**：检索金标 **20 → 120 条**（6 类题型：事实 45 / 多跳 20 / 跨文档 20 / 时序 15 / 否定 10 / 矛盾 10）、**新增问答金标 60 条**、语料 **7 → 26 篇**（含 10 组刻意矛盾）；**脚本生成 + 事实源**（`corpus_facts.py`）保证锚点不抄错、**脚本幂等**（基准集单独存）；**防回退守卫**（金标 <100 / 问答金标 <50 / 语料 <25 篇 / 锚点解析失败 → 红）；**基线重算**（recall@10 0.9545 / nDCG@10 0.836），历史结论标注「基于 20 条」② **`LM-03` 多模态并发闸门**：视觉直答 / caption / OCR / 图像向量**四处**纳入隔离仓（此前全部绕过闸门，可无限并发压上游）；**启动期许可和校验**（此前只在容量脚本里查，不跑脚本的人配错了也照样启动）；容量脚本同步。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.14；§二 总表由 **30 条 → 28 条**，`02` 篇 21 条 → 19 条。
- **2026-11-13（午·实施·八）**：**端点能力判定 + `ST-01` 阶段 A**——① **404 / 405 不再走通用重试**（embedding / rerank 的「服务没有这个端点」→ 专用错误码 5211 / 5212 + 可操作文案；401 仍是鉴权失败）；**启动期提示**「`MODEL_PROVIDER=deepseek` 时向量/重排实际回落 dashscope」（`effective_summary` 只看 provider 名会以为「换了模型检索也换了」）② **`ST-01` 阶段 A（上下文分区器 · 只观测）**：`services/context/` 升为包（`assembly` 装配 / `budget` 预算唯一真相源 / `partition` 分区器）+ `AskResult.context_breakdown`（五来源键齐全）+ `/admin/metrics/context`（跨请求聚合，**有界**）+ **死字段修复**（被丢弃条目列表接线）+ **阶段 A 门禁**（渲染与现有拼接逐字一致）。`ST-01` **仍在目录内**（阶段 B 结构分区依赖 `EV-03`），§二 总表仍为 **30 条**。
- **2026-11-13（晨·实施·七）**：**供应商解耦 + 安全 + 降级**（3 条）——① **DeepSeek 接入**（OpenAI 兼容 `/chat/completions`，真实 key 实测调通）+ **三个 provider 解耦**（生成 / 向量 / 重排各自独立，`resolved_*_provider` 负责回落；**DeepSeek 不提供 embeddings / rerank**，实测 `/embeddings` → 404，故 auto 模式下向量与重排回落 dashscope）+ `openai_compatible` 兼容通道（任何提供 `/embeddings` / `/rerank` 的服务）② **`B0 KQ-27` 提示词注入两层防护**（提示词加固：声明在证据之前 + 逐条成对边界标记；入库打标 + 检索降权 0.25×，**不是排除**；检测前 NFKC + 去零宽字符防变形绕过；8 条注入样本集与检测器绑死）③ **`LM-01` 模型路由与降级**（`LLMError.retryable/degradable`：鉴权与参数错**不可降级**、熔断也不降级；候选链按模型名重建实例；独立低基数指标 `kquery_llm_degrade_total{reason}`；响应透出 `model_used` / `routing_degraded`）。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.12；§二 总表由 **32 条 → 30 条**。**凭据处置**：用户给的 DeepSeek key 写入 `backend/.env`（gitignore，不入库），`.env.example` 由 `Settings` 生成、key 位留空。
- **2026-11-13（晨·实施·六）**：**第四梯队补尾 + 运维资产落地**（4 条）——`A-14` 模块级 import 卫生（守卫：只减不增 / 无环 / **46 处延迟导入全部补上「为何延迟」的理由标注**）· `C-08` 图存储锁等待指标（`_TimedLock` + `kquery_graph_lock_wait_seconds` / `kquery_graph_lock_hold_seconds`，判据「锁等待 P95 > 锁持有 P95 的 1/3」）· `C-06` 前端背压提示（`Retry-After` 解析抽成同源 helper，429/503 信封带可重试秒数，**500 不带**）· `OP-01` on-call runbook（8 张处置卡 + 分级 + SLO + 备份脚本 + 恢复演练 + 覆盖守卫）。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.11；§二 总表由 **36 条 → 32 条**，`01` 篇 13 条 → 9 条，`02` 篇 24 条 → 23 条。
- **2026-11-12（夜·实施·五）**：**并发线与结构线落地**（6 条）——`A-06` 入库任务淘汰（终态保留 30 分钟后淘汰，执行中的永不淘汰）+ `A-13` import 边界守卫（`core/` 不得反向 import `services/`，AST 判定）+ `C-07` 许可占用率（补分母 `kquery_upstream_permits` + 补多模态通道）+ `C-10` 上游 P95 降级判据（阈值重新标定 2000→8000ms，样本不足不降级，新增 `kquery_degrade_level`）+ `C-11` 流量优先级（低优先级队列上限更小 + 后台端点 503 + `Retry-After`）+ `C-12` 生成降级模板（上游不可用返 200 + 拒答文案，不再 5xx）。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.10；§二 总表由 **42 条 → 36 条**，`01` 篇由 18 条 → 13 条。
- **2026-11-12（夜·实施·四）**：**第四梯队补充落地**（9 条）——`A-15` 统一日志入口（全仓 79 个模块改用 `get_logger`，裸 `logging.getLogger` 归零）+ `A-16` 启动期脱敏配置回显+ `A-17` 阶段耗时汇总（`nodes={cache:2, retrieval:180, ...} total=1853`）+ `A-18` `extra` 截断 512+ `A-21` 死代码清理（9 条：6 处删除、3 处接线——`BackgroundQueue` 接入入库服务并带来上下文传播、`_context_dropped` 进响应、`ToolRegistry.specs()` 由 `/admin/tools` 消费）+ `A-22` 失真注释修正（5 条，其中「检查点 TTL 清理」补了周期任务）+ `A-23` 端点 docstring 覆盖 59.4% → **100%** + `A-24` 返回标注覆盖 86.8% → **95.9%** + `A-25` 前缀一致性守卫。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.9；§二 总表由 **51 条 → 42 条**，`01` 篇由 27 条 → 18 条。
- **2026-11-12（夜·实施·三）**：**第四梯队前半落地**（2 条）——`A-07` 领域异常体系（7 个派生异常按「调用方该怎么做」分类；裸 `raise RuntimeError/ValueError` **39 → 1**；`DomainError` 处理器按位置纪律插在 `ApiError` 之后、兜底之前，一次异常只在一处记堆栈）与 `A-08` 兜底三档（全仓 191 处宽兜底逐个体检，45 处真 C 档补痕迹、66 处补「为何能吞」理由，AST 守卫测试防 C 档复活）。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.8；§二 总表由 **53 条 → 51 条**，`01` 篇由 29 条 → 27 条。
- **2026-11-12（夜·实施·二）**：**第三梯队落地**（2 条）——`A-09` 错误指标（`kquery_errors_total{code,kind}`，计数点在唯一的异常处理器 + 无感降级上报点）与 `A-10` 死码裁决（**49 个码死位清零**：12 个接线到真实场景，3 个登记保留理由，守卫 `tests/unit/test_error_code_liveness.py` 守住不变量）。按纪律 D5 从本目录移除，现状见 `doc/analysis/14` §3.0.7；§二 总表由 **55 条 → 53 条**，`01` 篇由 31 条 → 29 条。<br>顺带修掉的一个真问题：`/ask` 的兜底异常路径此前返回「**200 + 错误信封**」，网关按 5xx 统计时全部漏掉（同 X-04 对 LLM 失败的处理），已改为 500。
- **2026-11-12（夜·实施）**：**第一、二梯队落地**（6 条）——`A-03` `backend/.gitignore` · `A-19` `gen_env_example.py` + CI 校验 · `A-20` 写端点限流（22 个端点补挂）+ 自动断言 · `A-04` 生产禁用 `/docs` · `A-05` `/healthz` 统一错误码 · `A-11` 真相源损坏拒绝启动。按纪律 D5，这 6 条**从本目录移除**，现状与锚点登记在 `doc/analysis/14` §3.0.6；§二 总表由 **61 条 → 55 条**，`01` 篇由 37 条 → 31 条。<br>顺带：① 目录由 design 改名为 `doc/plan/`，全仓文档交叉引用已同步（`scripts/check_doc_refs.py` 通过）；② 规模守卫 `test_multimodal_corpus.py` 不再硬编码「74 文件 / 1441 用例」，改为「文件数相等 + 用例数不低于基线」——硬编码会让守卫从「文档与现实一致」退化成「文档与历史快照一致」。
