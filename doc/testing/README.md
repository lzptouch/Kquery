# Kquery 测试计划 · 索引

> **本目录定位**：Kquery 的**测试计划**（做什么、怎么测、判定依据是什么）——不是测试代码，也不是测试结果。
> **配套文档**：系统现状分析在 [`../analysis/`](../analysis/README.md)（关键数字表在 `../analysis/README.md` §一）；未实现项的目标态设计在 [`../design/`](../design/README.md)。
> **编制日期**：2026-10-30（v1）· **代码基线**：git `0363f12`（2026-10-28）

---

## 一、文档清单

| 文件 | 职责 | 体量 | 主要读者 |
| --- | --- | --- | --- |
| [`00-全面测试计划.md`](./00-全面测试计划.md) | **主计划**：被测形态 · 工具选型 · 功能点矩阵 · 14 组用例表 · 结果规范 · 错误分析 · CI 分层 · 落地顺序 | ~28 KB | 所有人 |
| [`01-文件格式解析测试矩阵.md`](./01-文件格式解析测试矩阵.md) | **格式轴展开**：52 个扩展名全景 · 8 条通用解析契约 · **PDF 专项 30 例 / 13 类样本** · 其余 9 组格式 · 夹具建设方案 · 风险台账 | ~22 KB | 解析 / 入库开发者 |
| 本文 | 索引 · 执行入口 · 用例↔脚本↔层次映射 · 落地进度 | ~8 KB | 执行者 |

---

## 二、一句话结论

> **Kquery 的最大测试缺口不是"用例不够多"，而是"格式解析与检索质量两条链路都没有可量化的回归标尺"。**
>
> - 格式侧：52 个扩展名只有 **3 个测试文件**（17 例）覆盖，且**全部现场造理想文件**，真实世界的畸形 PDF（双栏 / 扫描 / 加密 / 损坏）**从未被测过**；
> - 质量侧：混合检索、RRF、重排、CRAG、自检机制齐备，但**无金标集** → recall@k / nDCG **无从测量**（`../analysis/12-测试与验证.md` §八）；
> - 假绿侧：`e2e_ui` 在端口不通时 **skip**（`backend/tests/e2e_ui/conftest.py:81-83`）→ 必须统计并公示 skip 数，否则"全绿"可能只是"没跑"。

**因此本计划的第一门禁是格式专项（PDF 30 例），第二门禁是安全组（越权零容忍），第三门禁是部署面。**

---

## 三、执行入口

### 3.1 现状（可直接跑）

```bash
# 后端四层
cd backend && uv run pytest tests/unit            # 369
cd backend && uv run pytest tests/integration     # 565
cd backend && uv run pytest tests/e2e             # 19
cd backend && uv run pytest tests/e2e_ui          # 32（端口不通则 skip —— 需注意）

# 前端
cd frontend && npm test                          # 48 / 8 文件
cd frontend && npm run typecheck:test

# 脚本层（base 必须显式给，缺口 G2）
python scripts/smoke_api.py --base http://127.0.0.1:8100 --token "$JWT"
python scripts/api_sweep.py --base http://127.0.0.1:8100
python scripts/load_test.py --api  http://127.0.0.1:8100 --password "$PW" --connections 50
```

### 3.2 已交付（格式专项，S1~S2）

```bash
# 格式专项（407 例：52 个扩展名 × 通用契约 + PDF/各格式专项 + 跨格式一致性）
cd backend && uv run pytest tests/format -v
cd backend && uv run pytest tests/format/test_pdf_matrix.py -v      # PDF 专项 22 例
cd backend && uv run pytest tests/format/test_all_extensions.py -v  # 52 个扩展名 257 例
```

### 3.3 待建（本计划交付，⛔ 当前不可用）

```bash
# 统一运行器（S3 交付）
node doc/testing/run-all.mjs
node doc/testing/run-all.mjs --only format
node doc/testing/run-all.mjs --skip e2e-ui
node doc/testing/cleanup.mjs --dry-run
```

> ⛔ **以上命令当前会失败（脚本尚不存在）**。这是**计划**，不是已完成项。

### 3.3 部署态

```bash
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:8100/readyz | python3 -m json.tool
docker compose down        # 保留卷
docker compose down -v     # 连卷一起删（会丢向量索引与审计日志）
```

---

## 四、基线口径（2026-10-30 实测）

> ⚠️ 与 `../analysis/README.md` §1.1 存在差异，以本表为准（差异说明见 §6）。

| 维度 | 实测值 | 核实方式 |
| --- | ---: | --- |
| 后端用例（收集） | **1395** | `uv run pytest --collect-only` |
| └ unit | 369 | `pytest --collect-only tests/unit` |
| └ integration | 568 | `pytest --collect-only tests/integration` |
| └ e2e | 19 | `pytest --collect-only tests/e2e` |
| └ e2e_ui | 32 | `pytest --collect-only tests/e2e_ui` |
| └ **format（本轮交付）** | **407** | `pytest --collect-only tests/format` |
| 后端测试文件 | 62 个 `test_*.py` | `backend/tests/**` |
| 前端用例 | **54** / 9 文件（全绿） | `npx vitest run` |
| 端点 | 89（13 router × 84 + 内建 5） | `../analysis/README.md` §1.2 |
| 测试脚本 | 7 | `scripts/` |
| CI job | 4 | `.github/workflows/ci.yml` |
| 代码基线 | git `0363f12`（2026-10-28） | `git log -1` |
| **本计划新增** | **265 例**（其中格式专项 100 例 / PDF 30 例） | 本文 §五 |

> **本轮（S1~S2）已交付**：格式专项实际落地 **407 例**（计划目标 100 例）。
> 超出计划的部分主要是三样：
> ① **52 个扩展名逐一过通用契约**（C1~C8 × 52 = 257 例）——计划只写了"逐格式"，
>    落地时按扩展名铺开，才算真正回答"支持哪些文件、每个都行不行"；
> ② **C5/C6/C12 扩展契约**（表格独立成块 / 表格不混入正文 / 幂等性）——
>    计划把它们列在"扩展契约"里，落地时发现**每种格式都要过**，于是参数化；
> ③ **跨格式一致性 XFM-08 逐扩展名**（52 例）。
>
> 全量后端：**1395 例 / 0 失败 / 38 跳过**，覆盖率 **85.19%**（门槛 80%）。

---

## 五、用例总览

| 组 | 前缀 | 用例数 | 覆盖层 | 文档位置 |
| --- | --- | ---: | --- | --- |
| 内核契约 | `KRN-*` | 17 | unit + integration | [主计划 §4.1](./00-全面测试计划.md) |
| 入库链路 | `ING-*` | 20 | integration | [主计划 §4.2](./00-全面测试计划.md) |
| 问答链路 | `ASK-*` | 15 | integration | [主计划 §4.3](./00-全面测试计划.md) |
| 检索链路 | `RET-*` | 14 | integration | [主计划 §4.4](./00-全面测试计划.md) |
| 校验与拒答 | `VAL-*` | 11 | integration | [主计划 §4.5](./00-全面测试计划.md) |
| 图谱与多模态 | `GRF-*` | 12 | integration | [主计划 §4.6](./00-全面测试计划.md) |
| 安全与治理 | `SEC-*` | 21 | integration | [主计划 §4.7](./00-全面测试计划.md) |
| 运维 | `OPS-*` | 10 | integration | [主计划 §4.8](./00-全面测试计划.md) |
| 前端 | `WEB-*` | 17 | vitest + e2e_ui | [主计划 §4.9](./00-全面测试计划.md) |
| 全栈 | `E2E-*` | 7 | e2e | [主计划 §4.10](./00-全面测试计划.md) |
| 部署 | `DEP-*` | 10 | compose | [主计划 §4.11](./00-全面测试计划.md) |
| 性能并发 | `PERF-*` | 6 | 脚本 | [主计划 §4.12](./00-全面测试计划.md) |
| 结果质量 | `RES-*` | 5 | quality（⛔ 待建） | [主计划 §4.13](./00-全面测试计划.md) |
| **格式专项（非 PDF）** | `DOCX/XLSX/PPTX/MD/TXT/HTML/CODE/IMG/AUD-*` | 60 | format | [矩阵 §四~§八](./01-文件格式解析测试矩阵.md) |
| **PDF 专项** | `PDF-*` | 30 | format | [矩阵 §三](./01-文件格式解析测试矩阵.md) |
| **跨格式一致性** | `XFM-*` | 10 | format | [矩阵 §九](./01-文件格式解析测试矩阵.md) |
| **合计** | | **265** | | |

> 数字口径：**本计划新增 265 例**（现有 985 + 48 用例之外）；其中格式专项 100 例。
> 优先级分布：主计划 165 例 = P0 **57** / P1 **90** / P2 **11** / ⛔ **7**；格式专项 100 例 = P0 **50** / P1 **38** / P2 **10** / ⛔ **2**。
> `⛔` 合计 **9 例**（`RES-01~05` 5 例 · `RET-14` · `GRF-11` · `IMG-05` · `AUD-05`）—— 不可自动化，须**人工签字**并留证。

---

## 六、与 `../analysis/` 的口径差异（**需要回写**）

| # | 项 | `../analysis/README.md` §1.1 | 本计划实测 | 处置 |
| --- | --- | --- | --- | --- |
| 1 | 后端用例总数 | 974 | **985 → 1395**（S1~S2 后） | 回写 `../analysis/README.md` 与 `../analysis/12-测试与验证.md` |
| 2 | e2e_ui 用例数 | 21 | **32** | 同上 |
| 3 | 后端测试文件数 | 58 | **57 → 62**（`test_*.py`） | 明确口径：是否含 `conftest.py` |
| 4 | 前端用例数 | 48 | 48 → **54** ✅ | 根 `README.md:38` 写 39 → 回写 |

> **纪律**：本计划的数字**不再手写**——S7 阶段改为由 `report.json` 生成，`../analysis/` 侧只引用不复制。

---

## 七、落地进度

| 阶段 | 动作 | 状态 | 产出 |
| --- | --- | --- | --- |
| **S1** | `backend/tests/format/` 骨架 + `fixtures.py` 造件工厂 + `assert_parse_contract` 断言器 | ✅ **已交付** | `backend/tests/format/{fixtures,contract,conftest}.py`；造件工厂覆盖 10 个格式族 / 52 个扩展名 + PDF 13 类样本 |
| **S2** | `test_pdf_matrix.py`（30 例 / 13 类样本）+ `test_other_formats.py` | ✅ **已交付** | 格式专项 **407 例 / 0 失败 / 5 跳过**；**第一门禁达成**；顺带修掉 2 个真实缺陷（见矩阵 §十二 新增 #10 #11） |
| **S3** | `doc/testing/lib/harness.mjs` + `run-all.mjs` + `report-merge.mjs` + 统一 7 脚本 base（修 G2） | ⛔ 未开始 | 一条命令跑完全层 |
| **S4** | e2e_ui「端口不通 → 失败而非 skip」开关；补 WEB 组缺口（守卫单测 / SSE 白名单 / PDF 上传配置项） | ⛔ 未开始 | 消除假绿 |
| **S5** | CI 加 `e2e` + `format` + Docker job（修 G1）；`--cov-fail-under`（修 G3） | ⛔ 未开始 | 部署面进流水线 |
| **S6** | `backend/tests/fixtures/` 真实样本层 + `backend/tests/quality/` 金标集骨架 | ⛔ 未开始 | 真实格式覆盖 + 质量入口 |
| **S7** | 回写 `../analysis/12` 与 `../analysis/README.md` 数字 | ⛔ 未开始 | 文档不漂移 |

### S1~S2 的产出清单

| 文件 | 用例数 | 覆盖 |
| --- | ---: | --- |
| `tests/format/test_all_extensions.py` | 257 | **全部 52 个扩展名** × 路由/白名单/通用契约/幂等；C2、XFM-01 |
| `tests/format/test_xformat.py` | 68 | XFM-01~10 跨格式一致性 |
| `tests/format/test_other_formats.py` | 47 | DOCX / XLSX / PPTX / MD / TXT / HTML / CODE / IMG / AUD |
| `tests/format/test_pdf_matrix.py` | 22 | PDF 专项（13 类样本） |
| `tests/format/test_format_e2e.py` | 13 | C8 可检索回读 + PDF-20~22 / 27~30 + XFM-06 |

> 5 条跳过各有明确原因（**不是"没跑"**）：`.doc` 幂等与 XFM-08（需 LibreOffice）、
> DOCX-10（环境依赖）、IMG-05 / AUD-05（⛔ 人工签字用例）。

> **S6 依赖**：`../design/02-检索质量度量设计.md` 的金标集格式需先定稿。

---

## 八、维护触发

本目录必须更新的条件：

- `backend/app/services/parsers/**` 任一变更 → 更新 [`01`](./01-文件格式解析测试矩阵.md) §一、§三 及主计划 §4 的格式类用例；
- `backend/app/core/config.py` 中任一限额默认值变化 → 更新两篇的限额表；
- `backend/app/api/**` 新增/删除端点 → 更新主计划 §4.1 `KRN-API-01` 的端点数；
- `frontend/src/lib/file-types.ts` 白名单变化 → 更新 [`01`](./01-文件格式解析测试矩阵.md) §1.1 与 XFM-01 守卫；
- `backend/tests/**` 新增/删除测试文件 → 更新 §四 基线表；
- `scripts/**` 新增/删除脚本 → 更新主计划 §7.1 与 §7.3 的 base 映射。

**上次核对**：2026-10-30（S1~S2 交付后）· git `0363f12`
