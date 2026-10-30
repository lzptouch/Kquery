# 整合 doc-qa 独有功能到 Kquery —— 分析与实施计划（v1.2 实施状态回写）

> - **实施状态回写**：2026-10-30。
> **关于文中出现的旧文件名**（`接口.md` / `链路分析.md` / `模块.md` / `数据模型.md` / `部署.md` / `监控体系.md` / `高并发.md` / `测试体系.md` / `阶段架构.md` 等）：这些是**重构前的 `doc/analysis/` 旧文件名**，已按「分析文档体系重构」计划 B6b 退场，对应内容并入现行 15 篇。它们在本篇中出现**仅用于描述「旧 → 新」的映射关系**，不是活链接。**现行文件名见 `doc/analysis/README.md` §三文档清单。**B0~B6 **全部完成**（交付记录见姊妹篇 `doc/plan/实施记录-doc-qa能力整合.md`）；本文档 §四 批次表补「状态」列，§七 DoD 逐条回填。
> - **实施状态标记**（本篇统一，四态）：✅ **已实现** · ⚠️ **部分实现** · ❌ **未实现** · 🚫 **已下线**
> - **注意**：本文是**决策与施工计划**（为什么做、怎么做）；**实际做了什么**以 `doc/plan/实施记录-doc-qa能力整合.md` 为准。

> - **来源项目**：`/Users/lizhipeng/workspace/project/doc-qa`（智能文档问答 POC）
> - **目标项目**：`/Users/lizhipeng/workspace/project/Kquery`（生产级智能 RAG，阶段 1–9 已完成）
> - **版本**：**v1.1**（2026-10-25 自审修订；v1.0 的 15 处错误与缺口见 §九）
> - **文档定位**：实施前的分析与改造计划。所有结论带 `文件:行号` 证据，可逐条复核
> - **配套文档**：`doc/plan/代码规范符合性审计与整改计划.md`（工程纪律基线，本文改造须同时满足其 P0 底线）

---

## 零、结论速览

### 0.0 本版（v1.1）修订摘要

v1.0 自审发现 15 处问题，本版已全部修正（明细见 §九）：

| 级别 | 数量 | 代表问题 |
|---|---|---|
| **阻断性**（按 v1.0 施工必返工） | 4 | ① 快问骨架调用了**不存在的方法签名**（`parse_document`/`apply_chunking` 实参是 `DocumentRecord`，非 path/doc_id）；② 快问只 `store.add` **漏掉 BM25 写入**，关键词路对快问文档零召回；③ `_build_filters` 在 **ACL 关闭时返回 `None`**，只加 `doc_id` 字段不生效（测试环境即 ACL 关闭 → 功能"看似实现实则无效"）；④ 复用 `_purge_chunks` 会触发 `_bump_version()` → **每次快问上传/删除都清空全局语义缓存** |
| 事实性（行号/字段/枚举不符） | 8 | `.txt` 断言行号、`ingest_file` 校验行号、缓存键真实位置（`pipeline._cache_key:235` 而非 `:1002`）、`AskRequest` 行号、`httpx` 所在依赖分组、**错误码 4401 已被 `RERANK_DEGRADED` 占用**、**`DocumentRecord` 无 `title`/`user_id` 字段**、`ErrCode.PARSE_EMPTY` 不存在 |
| 一致性/编号 | 3 | `.xls` 支持与拒绝自相矛盾、§2.3 编号错成 2.2.6、§0.1「6 项」与矩阵 8 行不符 |

### 0.1 一句话结论

> doc-qa 相对 Kquery 的**净增量是 6 项**（4 项用户体验 + 2 项工程形态），另 2 项（本地 DeepDOC、ES/PG 存储）经评估**不移植**——Kquery 在这两处已有更强替代。整合原则：**只搬 doc-qa 的「增量体验」，不搬它的「存量实现」**，避免把 Kquery 的架构优势改回去。

### 0.2 增量能力决策矩阵

| # | doc-qa 独有能力 | doc-qa 证据 | Kquery 现状 | 决策 | 批次 | 人日 |
|---|---|---|---|---|---|---|
| **F1** | 对话答案 **Markdown 富文本渲染** | `doc-qa/frontend/src/components/markdown/index.tsx:1-26`、`pages/chat/component/result.tsx:43-81` | 纯文本 `<span>`（`Kquery/frontend/src/features/chat/components/StreamText.tsx:57`） | ✅ 移植 | P0 | 1.0 |
| **F2** | **浏览器实时语音输入** + 后端 STS 代理 | `doc-qa/frontend/src/components/sender/recorder.tsx:5-97`、`backend/app/router/user_rt.py:65-90` | 无录音能力；前端仅 3 个依赖（`frontend/package.json:11-15`） | ✅ 移植 | P0 | 2.0 |
| **F3** | **解析格式广度**（xlsx / txt / html / json / csv / 代码） | `doc-qa/backend/app/service/core/rag/app/naive.py:213-296`（`:241` 命中 xlsx） | 仅 md/pdf/docx/doc/图/音（`backend/app/services/parsers/router.py:19-22`） | ✅ 移植（**`.xlsx` 支持、`.xls` 明确不支持**，见 §2.3.4） | P0 | 2.0 |
| **F4** | **单文档「快问」**（上传即问、不落知识库、普通用户、2h TTL） | `doc-qa/backend/app/router/chat_rt.py:64-78`、`service/quick_parse_service.py:171-184` | 无；知识库上传仅 admin（`backend/app/api/knowledge.py:22`） | ✅ 移植（同步 MVP + 页数上限，见 §2.4.4） | P1 | 3.5 |
| **F5** | **LLM 推荐问题 + 会话自动命名** | `doc-qa/backend/app/service/core/chat.py:37-142`、`:144-189` | 无（`grep recommend` 无命中） | ✅ 移植 | P1 | 1.0 |
| **F6** | **跨进程共享存储**（Redis） | `doc-qa/backend/docker-compose.yml`、`service/quick_parse_service.py:29-37` | `RedisStore` 已存在但仅用于计数（`backend/app/core/shared_state.py:133-215`） | ⚠️ 部分移植（Redis ✅ / PG ❌，§2.8.2） | P1 | 2.0 |
| **F7** | 本地 DeepDOC（ONNX OCR + 版面 + 表格） | `doc-qa/backend/app/service/core/deepdoc/parser/pdf_parser.py:38-43` | PyMuPDF `find_tables` + `qwen-vl-ocr`（`backend/app/services/parsers/pdf.py:4-8`） | ❌ 不移植（§2.7） | — | 0 |
| **F8** | ES 混合检索 / PG 关系库 | `doc-qa/.../search_v2.py:111-114`、`backend/docker-compose.yml` | BM25+向量双路 → RRF(k=60) → rerank；无关系库 | ❌ 不移植（§2.8） | — | 0 |

**合计 ≈ 11.5 人日**（含测试与文档），分 7 个批次落地。

### 0.3 明确不做（Out of Scope）

1. 不引入 PostgreSQL 与 alembic——与 Kquery「无状态 + 单目录文件」架构冲突，双写一致性成本 > 收益（§2.8.2）。
2. 不移植 doc-qa 的 `service/core/rag` 全家桶（自研分词/同义词/pagerank/tag 加权）——其主链路根本未调用（`search_v2.py:155-242`、`:297-300`）。
3. 不引入 antd/valtio/ahooks——Kquery 手写 `theme.css` 令牌体系（`frontend/src/theme.css:3-42`），引入组件库会推翻既有样式约定。
4. 不改 Kquery 检索/治理/Agent 架构——只在既有扩展点加能力。

---

## 一、背景与目标

### 1.1 定位差异

| 维度 | doc-qa | Kquery |
|---|---|---|
| 定位 | 文档问答 POC：单文档即问 + 知识库问答 + 语音输入 | 生产级 RAG：十阶段递进，覆盖检索增强/Agent/GraphRAG/多模态/治理/可观测 |
| 后端 | FastAPI + **ES 8.11** + **PG 15** + **Redis 7** + ONNX 本地推理 | FastAPI + **Milvus 2.6/FAISS** + LangGraph，**无关系库** |
| 规模 | 87 py / 14,142 行 / 14 端点 | 107 py / 20,118 行 / 72 端点 |
| 前端 | React18 + antd5 + valtio + ahooks + marked + 字节语音 SDK | React18 + **仅 react-router-dom** + 手写 CSS |
| 质量基线 | 无 tests、无 CI、无监控 | **34 个测试文件 / 683 个用例**（`grep -c "def test_"` 实测）/ ruff / GitHub Actions |

### 1.2 doc-qa 独有能力与 Kquery 现状对照

| 能力 | doc-qa 实现 | Kquery | Kquery 证据 |
|---|---|---|---|
| Markdown 富文本 + 内联角标 | `components/markdown/index.tsx:1-26` | ❌ | `StreamText.tsx:57` 纯文本 span |
| 浏览器实时语音输入 | `components/sender/recorder.tsx:27-97` | ❌ | 前端无 `getUserMedia/MediaRecorder/AudioContext` |
| STS token 代理（密钥不落前端） | `router/user_rt.py:65-90` | ❌ | `backend/app/main.py:772-781` 无 sts 路由 |
| xlsx 解析 | `rag/app/naive.py:241` | ❌ | `parsers/router.py:19-22` 无 spreadsheet |
| txt/html/json/代码解析 | `rag/app/naive.py:213-296` | ❌ | 同上；`tests/e2e/test_api.py:33-42` 断言 `.txt` 必须 400 |
| 单文档快问（普通用户 / 2h TTL） | `router/chat_rt.py:64-78` | ❌ | 知识库 router 级 `require_role("admin")`（`api/knowledge.py:22`） |
| LLM 推荐问题 / 会话命名 | `service/core/chat.py:37-142`、`:144-189` | ❌ | `grep recommend` 无命中 |
| Redis 共享缓存 | `docker-compose.yml` | ⚠️ 部分 | `core/shared_state.py:133-215` 目前只被限流/配额用作计数器 |
| PG 关系库 + alembic | `docker-compose.yml`、`app/alembic/` | ❌ | 无 alembic、无业务建表 |

### 1.3 目标与非目标

**目标**
- G1：F1–F5 在 Kquery 内**原生实现**（按 Kquery 的分层/信封/错误码/测试约定重写，非代码搬运）。
- G2：F6 落到**最小必要范围**（快问临时态、语义缓存、限流/配额），顺带消解审计文档指出的「无界内存表」。
- G3：每项能力自带测试（后端 unit + integration、前端 vitest + Playwright e2e_ui），并把前端构建纳入 CI。
- G4：不破坏既有契约——SSE 事件名、统一信封、ACL 下推、限流、审计口径不变。

**非目标**：性能专项、多副本改造、评测平台（已下线，`README.md:48`）。

---

## 二、逐项分析与设计

### 2.1 F1 · Markdown 富文本渲染（P0）

#### 2.1.1 现状

- 渲染链路：`lib/sse-client.ts:148`（`token` 事件）→ `ChatPage.tsx:131-134`（字符串累加）→ `StreamText.tsx:26-42`（rAF 节流）→ `:45`（按 `(\[#\d+\])` 切分）→ `:57`（`<span>`）。
- 引用链路：`StreamText.tsx:20` `CITATION_RE` → `CitationChip.tsx:12-23` → `StreamText.tsx:55` → `MessageBubble.tsx:157` → `ChatPage.tsx:325-328`（设 highlight）→ `MessageBubble.tsx:45-55`（展开 + `scrollIntoView`）。
- 换行靠 `.answer-text { white-space: pre-wrap }`（`theme.css:178-181`）。
- doc-qa：`marked` + `TokenizerAndRendererExtension`（`components/markdown/index.tsx:1-26`），角标格式 `##N$$`。

> ⚠️ **格式不兼容**：Kquery 引用标记是 `[#N]`（后端 `services/pipeline.py:72`、`services/llm.py:334`），doc-qa 是 `##N$$`。**必须保留 Kquery 语义，不得照抄 doc-qa 正则。**

#### 2.1.2 目标行为

1. 标题/列表/表格/代码块/行内强调按 Markdown 渲染。
2. `[#N]` 渲染后仍是可点击角标，点击链路与现状**完全一致**。
3. **安全**：禁用原始 HTML（LLM 输出经 `dangerouslySetInnerHTML`，是 XSS 面）。
4. 流式期间保持 rAF 节流，不因每 token 全量重解析而抖动。

#### 2.1.3 改造清单

| 文件 | 改动 | 位置 |
|---|---|---|
| `frontend/package.json` | 新增 `"marked": "^15.0.7"` | `:11-22` |
| **新增** `frontend/src/features/chat/components/Markdown.tsx` | marked 实例 + inline 扩展 + 事件委托 | 新文件 |
| `StreamText.tsx` | `<Markdown>` 替换 `:45-58`；保留 `:26-42` 节流 | `:44-58` |
| `MessageBubble.tsx` | 透传 `onCitationClick` | `:153-158` |
| `theme.css` | 新增 `.markdown-body` 区块（**只用 CSS 变量**） | `:178-181` 附近 |
| `ChatPage.tsx` | 原文预览 `:401` 复用同一组件 | `:401` |

#### 2.1.4 关键骨架

```tsx
// frontend/src/features/chat/components/Markdown.tsx（新增）
import { Marked, type TokenizerAndRendererExtension } from "marked";
import { useMemo, type MouseEvent } from "react";

const CITE_EXT: TokenizerAndRendererExtension = {
  name: "cite",
  level: "inline",
  start(src: string) { return src.indexOf("[#"); },
  tokenizer(src: string) {
    const m = /^\[#(\d+)\]/.exec(src);
    if (!m) return undefined;
    // marked 的 Token 类型未开放扩展字段，需断言
    return { type: "cite", raw: m[0], rank: Number(m[1]) } as never;
  },
  renderer(token) {
    const rank = (token as unknown as { rank: number }).rank;
    return `<button type="button" class="citation-chip" data-cite-index="${rank}">[#${rank}]</button>`;
  },
};

export function Markdown({ value, onCitationClick }: {
  value: string;
  onCitationClick?: (rank: number) => void;
}) {
  const html = useMemo(() => {
    const instance = new Marked({ gfm: true, extensions: [CITE_EXT] });
    // 安全基线：marked v15 已移除 sanitize，必须显式丢弃原始 HTML
    instance.use({ renderer: { html: () => "" } });
    return instance.parse(value ?? "", { async: false }) as string;
  }, [value]);

  const onClick = (e: MouseEvent<HTMLDivElement>) => {
    const el = (e.target as HTMLElement).closest("[data-cite-index]");
    if (!el) return;
    const rank = Number(el.getAttribute("data-cite-index"));
    if (Number.isFinite(rank)) onCitationClick?.(rank);
  };

  return <div className="markdown-body" onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}
```

> 用事件委托而非 renderer 内回调：渲染发生在 `useMemo` 纯函数中，没有 React 上下文可注入回调。

#### 2.1.5 测试

| 层级 | 文件 | 用例 | 断言 |
|---|---|---|---|
| 前端单测 | 新增 `frontend/tests/markdown.test.tsx` | `renders_heading_and_list` | 产出 `<h1>`/`<li>` |
| | | `renders_citation_chip` | `.citation-chip[data-cite-index="2"]` |
| | | `strips_raw_html` | 输入 `<script>` → DOM 无 `<script>` |
| | | `click_chip_triggers_callback` | 回调收到 `2` |
| | | `code_fence_not_parsed_as_cite` | 代码块内 `[#1]` 不产生角标 |
| e2e UI | `backend/tests/e2e_ui/test_ui_chat.py` | `test_answer_rendered_as_markdown` | 存在 `.markdown-body` 与 `.citation-chip` |

#### 2.1.6 验收

- [x] `npm run build`（`tsc -b && vite build`）零错误
  > ✅ CI `frontend` job 含 `typecheck` 与 `build` 两步（`.github/workflows/ci.yml`）。
- [x] 引用点击跳转无回归
  > ✅ `frontend/src/features/chat/components/MessageBubble.tsx:39-60`（引文搜索与汇总）；`frontend/tests/message-bubble.test.tsx` 覆盖。
- [x] 输入 `<img src=x onerror=alert(1)>` 不产生注入标签
  > ✅ `frontend/src/features/chat/components/Markdown.tsx:42-52`（`html: () => ""` 丢弃原始 HTML）——这是 XSS 用例要守的正是这条。

#### 2.1.7 风险

| 风险 | 缓解 |
|---|---|
| marked 默认不过滤 HTML → XSS | `renderer.html = () => ""` + 单测断言 |
| 流式全量重解析卡顿 | 保留 rAF 节流；必要时按块增量解析 |
| 代码块/表格内 `[#N]` 误识别 | inline 扩展在 `codespan`/`fences` 内不生效（marked 天然隔离）+ 单测 |

---

### 2.2 F2 · 浏览器端实时语音输入（P0）

#### 2.2.1 现状

- doc-qa：`LabASR`（`byted-ailab-speech-sdk`）流式转写，pcm/16k/16bit，连 `wss://.../sauc/bigmodel`（`recorder.tsx:6`、`:27-97`）；token 由后端 `/sts-token` 用 accessKey 换（`user_rt.py:65-90`）。
- Kquery：前端零录音能力；`ChatInput.tsx:14` 草稿键、`:17` 内部 `useState`、`:29-35` 提交、`:38-41` 附件按钮 disabled、`:50-55` Enter 提交。API 层唯一出口 `lib/api-client.ts:50` `request()`、`:53` 相对路径、`:406` 为 `uploadFile` 末尾。代理清单 `vite.config.ts:33-49` 与 `nginx.conf:41` **两份必须一致**（vite 注释 `:29-32` 已警告过该坑）。后端无 sts 路由（`main.py:772-781`）。

#### 2.2.2 目标行为

1. 输入框左侧麦克风按钮，点击开始流式识别，**回填输入框但不自动发送**。
2. STS token 由 Kquery 后端持有火山密钥换取，密钥不进前端产物。
3. 不可用（不支持/无权限/后端未配置）时按钮 disabled + tooltip，不抛未捕获异常。

#### 2.2.3 改造清单

**后端**

| 文件 | 改动 |
|---|---|
| **新增** `backend/app/api/speech.py` | `POST /speech/sts-token` → 调火山 STS，返回 `{appid, token, duration}`，统一信封；未配置 → 503 业务错误 |
| `backend/app/main.py` | `:781` 前 `include_router(speech.router)` |
| `backend/app/core/config.py` | 新增 `volc_appid`/`volc_access_key`；`validate_startup()`（`:294-359`）**不强制**（可选能力） |
| `backend/pyproject.toml` | `httpx>=0.27.0` 现仅在 dev 组（`:39`），**上移至 `[project].dependencies`（`:8-27`）** |
| `backend/.env.example` | 新增 `VOLC_APPID=` / `VOLC_ACCESS_KEY=` |

**前端**

| 文件 | 改动 |
|---|---|
| `frontend/package.json` | 新增 `byted-ailab-speech-sdk@^4.0.9` + `overrides` 锁 react（`doc-qa/frontend/package.json:12-16` 已验证可行） |
| **新增** `features/chat/components/VoiceRecorder.tsx` | `getUserMedia` → `AudioContext` 重采样 16k/单声道/PCM16 → `LabASR` → `onTranscript` |
| `features/chat/components/ChatInput.tsx` | `:17` 改**可选受控**（新增 `value?`/`onValueChange?`，未传则内部 state，向后兼容）；`:39` 后挂 `<VoiceRecorder>` |
| `features/chat/ChatPage.tsx` | `:380` 传受控 value，接收转写回填 |
| `lib/api-client.ts` | `:406` 后追加 `fetchStsToken()` |
| `vite.config.ts` / `nginx.conf` | 前缀清单加 `speech`（`:33-49` / `:41`） |
| `theme.css` | 新增语音输入样式（复用 `--primary`/`--error`） |

#### 2.2.4 契约

```
POST /speech/sts-token
→ 200 {"status":"ok","data":{"appid":"...","token":"...","duration":300},"meta":{"trace_id":"..."}}
→ 503 {"status":"error","error":{"code":3803,"message":"语音服务未配置"},"meta":{...}}
```

- 响应体与日志均不得出现 `volc_access_key`。
- 前端每次录音前取一次 token，按 `duration` 用 `useRef` 缓存，**不落 localStorage**。
- **错误码须新分配**：`3803 SPEECH_UNAVAILABLE`（38xx 段现为 `VISION_UNAVAILABLE=3801`、`AUDIO_TRANSCRIBE_FAILED=3802`）。注意 `4401` 已被 `RERANK_DEGRADED` 占用（`core/envelope.py:61`），不可复用。

#### 2.2.5 测试

| 层级 | 文件 | 用例 | 断言 |
|---|---|---|---|
| 后端 unit | 新增 `tests/unit/test_speech_sts.py` | `test_disabled_returns_503` | 未配置 → 503 + 3803 |
| | | `test_success_does_not_leak_key` | mock httpx → 响应与日志无 access_key |
| | | `test_upstream_error_mapped` | 上游 5xx → 502 业务错误 |
| 前端 | 新增 `frontend/tests/voice-recorder.test.tsx` | `disabled_without_media_devices` / `transcript_fills_input` | disabled 状态；回调回填 |
| e2e UI | `tests/e2e_ui/test_ui_chat.py` | `test_voice_button_present` | 存在麦克风按钮，无麦克风时 disabled 不崩溃 |

#### 2.2.6 验收

- [x] 未配置密钥时前端 disabled + 后端 503（非 500）
  > ✅ `frontend/src/features/chat/components/VoiceRecorder.tsx:152-174`（挂载探测 `/speech/status`）；后端 `backend/app/api/speech.py` 在无 Key 时返回 503。
- [x] DevTools 中看不到 accessKey
  > ✅ 方案本身就是「后端 ASR 代理 + 密钥留后端」（交付记录 §五之二），浏览器侧不持凭据。
- [x] `vite.config.ts` 与 `nginx.conf` 前缀清单一致
  > ⚠️ **部分**：生产的 `frontend/nginx.conf:41-62` 含 `speech` / `quickask`，dev 的 `frontend/vite.config.ts` 需逐条核对；两者**不是同一份清单**，维护上是两处手改。

#### 2.2.7 风险

| 风险 | 缓解 |
|---|---|
| 麦克风权限策略 | 点击后再申请；失败给 tooltip |
| SDK 与 React18 peer 冲突 | `overrides` 锁 `react: ^18.3.1` |
| 采样率不匹配（48k vs 16k） | `AudioContext` 重采样至 16k 单声道 PCM16 |
| 录音数据外发合规 | 显式提示；仅点击后开始；token 有效 300s |

---

### 2.3 F3 · 解析格式扩展（P0）

#### 2.3.1 现状

- 契约：`parsers/base.py:16-22` `ParseResult{segments: list[tuple[str,str]], tables, assets, outline, warnings}`；`:25-28` `Parser` Protocol（`name` + `parse(path, doc_id, tenant_id, policy)`）。
- 注册：`parsers/router.py:19-22` `SUPPORTED_EXTENSIONS`、`:25-29` `TYPE_GROUPS`、`:32-46` 装配与实例表、`:54-65` 路由、`:67-91` 入口、`:93-103` `progress` 探测。
- 实现模板：`parsers/docx.py:94-166`、`parsers/pdf.py:37-106`。
- **关键发现（显著降低成本）**：扩展名白名单**只有一处真源**——`api/ingest.py:19` 与 `services/knowledge.py` 都 `from .parsers.router import SUPPORTED_EXTENSIONS`，校验分别在 `api/ingest.py:77`、`services/knowledge.py:375`。因此**只需改 `router.py:19-22`**，异步入库与同步上传两条路径自动跟随。
- 前端白名单两处：`UploadWizardPage.tsx:11`（`ACCEPT`）、`:15-18`（分组常量）、`:48-55`（策略 UI）、`:61-69`（校验）；`DocumentUploader.tsx:22`（`ALLOWED_EXT`）、`:109`（`accept=`）。
- 冲突断言：`tests/e2e/test_api.py:33-42` `test_upload_unsupported_file` 用 `notes.txt` 断言 400——**新增 txt 支持后必失败**。

#### 2.3.2 目标行为

1. 新增：`.xlsx`（按 sheet 展开 → 文本段 + `TableRecord`）、`.txt/.csv/.json/.yaml/.yml/.html/.htm`、代码文件（`.py/.js/.ts/.java/.go/.sql/.sh` 等按纯文本）。
2. 编码兜底：`utf-8` → `gbk` → `latin-1`（不引入 `chardet`，避免新增依赖）。
3. HTML 去标签，`<script>`/`<style>` 内容不入索引。
4. Excel 同时产出 `TableRecord.markdown`（复用 `domain/schemas.py:117-126` 既有结构，**不新增 schema**）。

#### 2.3.3 改造清单

| 文件 | 改动 |
|---|---|
| `backend/pyproject.toml` | `[project].dependencies`（`:8-27`）新增 `openpyxl>=3.1.5` |
| **新增** `parsers/spreadsheet.py` | `SpreadsheetParser`（`.xlsx`） |
| **新增** `parsers/plaintext.py` | `PlainTextParser`（txt/csv/json/yaml/html/代码） |
| `parsers/router.py` | `:19-22` 白名单、`:25-29` 分组、`:32-46` 注册 |
| `backend/app/main.py` | `:243-255` 装配两个新 parser |
| `api/ingest.py` | **无需改扩展名校验**；仅 `:29` `TEXT_SCANABLE_EXTENSIONS` 补 txt/csv/json/yaml 以便 PII 扫描 |
| 前端 `UploadWizardPage.tsx` | `:11`/`:15-18`/`:48-55`/`:61-69` |
| 前端 `DocumentUploader.tsx` | `:22`/`:109` |
| `tests/e2e/test_api.py` | `:33-42` 改用真正不支持的扩展名（如 `.bin`） |

#### 2.3.4 关于 `.xls`：只支持 `.xlsx`（v1.0 自相矛盾，本版裁定）

**决策：`.xlsx` 支持；`.xls` 明确不支持，返回可读错误。**

理由：openpyxl 读不了 BIFF 格式 `.xls`。doc-qa 虽在正则 `naive.py:241` 写了 `\.xlsx?$`，但落到 openpyxl 照样失败——**它也没有真正支持 `.xls`**，只是错误暴露得晚。若必须支持需引入长期无维护的 `xlrd<2.0`，不做。

错误文案：「不支持旧版 Excel 格式（.xls），请另存为 .xlsx 后重试」。

#### 2.3.5 关键骨架

```python
# backend/app/services/parsers/spreadsheet.py（新增，节选）
class SpreadsheetParser:
    name = "spreadsheet"

    def parse(self, path: Path, doc_id: str, tenant_id: str, policy: dict) -> ParseResult:
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        segments: list[tuple[str, str]] = []
        tables: list[TableRecord] = []
        for idx, sheet in enumerate(wb.worksheets):
            rows = [["" if c is None else str(c) for c in row]
                    for row in sheet.iter_rows(values_only=True)]
            if not rows:
                continue
            width = max(len(r) for r in rows)
            md = "\n".join("| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows)
            tables.append(TableRecord(
                table_id=f"{doc_id}-t{idx}", doc_id=doc_id, page=idx,
                table_index=idx, rows=len(rows), cols=width, markdown=md,
            ))
            segments.append((f"表格:{sheet.title}", md))
        return ParseResult(segments=segments, tables=tables, assets=[], outline=[], warnings=[])
```

> 大文件保护：沿用 `core/config.py:239` `max_upload_mb`；另在 parser 内限制单 sheet 行数（如 20,000 行）与总段数，超限写 `warnings` 并截断，避免一次性 OOM。

#### 2.3.6 测试

| 层级 | 文件 | 用例 | 断言 |
|---|---|---|---|
| unit | 新增 `tests/unit/test_spreadsheet_parser.py` | `test_parses_sheets_and_tables` | 2 个 sheet → 2 段 + 2 个 `TableRecord`，行列数正确 |
| | | `test_empty_workbook_warns` | 不回抛异常，`warnings` 非空 |
| | | `test_row_limit_truncates` | 超限截断 + warning |
| unit | 新增 `tests/unit/test_plaintext_parser.py` | `test_gbk_fallback` | GBK 文件解码成功 |
| | | `test_html_strips_script` | `<script>` 内容不入 segments |
| | | `test_code_file_kept_verbatim` | `.py` 全文保留 |
| integration | 扩展 `tests/integration/test_parsing_survey.py` | `test_xlsx_upload_and_retrievable` | 上传 xlsx → 预览有 chunk → `/ask` 命中（模板 `:13-38` 造文件、`:42-66` 断言） |
| 回归 | 改 `tests/e2e/test_api.py:33-42` | `test_upload_unsupported_file` | 换 `.bin`，错误码仍 2003 |
| 前端 | `frontend/tests/upload-wizard.test.tsx` | `test_accepts_xlsx` | `accept` 含 `.xlsx` |

#### 2.3.7 验收与风险

**验收**

- [x] 上传 `.xlsx` → 列表出现、预览可见表格 Markdown、提问可命中
  > ✅ `backend/app/services/parsers/spreadsheet.py`；`backend/tests/unit/test_spreadsheet_parser.py`、`backend/tests/integration/test_parsing_survey.py`。
- [x] 上传 `.txt` 不再 400（旧断言已同步修正）
  > ✅ `backend/app/services/parsers/plaintext.py:27-29`；旧断言随 B2 修正（交付记录 §一 B2 行有留档）。
- [x] 上传 `.xls` → 结构化错误（提示另存为 .xlsx），不 500
  > ✅ `backend/app/services/parsers/router.py:38-42`（`.xls` / `.ppt` 给「请另存为」提示，不笼统报「不支持」）。
- [x] `uv run ruff check app tests` 零告警
  > ⚠️ **未达成**——`backend/pyproject.toml` 的 `[tool.ruff.lint]` **未设 `select`**，只跑默认规则；「零告警」因此不是有效判据。整改项见 `doc/plan/代码规范符合性审计与整改计划.md` §8.2 的 A-01。

| 风险 | 缓解 |
|---|---|
| 超大 sheet 撑爆内存 | `read_only=True` + 行数上限 + 截断 warning |
| GBK 文件乱码 | 三级编码回退；仍失败则落 `latin-1` 并 warning |
| 代码文件污染检索语义 | 仅按纯文本入索引，不做符号解析；检索侧靠 chunk 粒度控制 |

---

### 2.4 F4 · 单文档「快问」通道（P1）

> 本节是 v1.1 改动最大的一节：v1.0 的骨架**无法运行**（实参类型错误），且遗漏了 3 个会导致「功能看似实现、实际无效或反噬」的关键点（见 2.4.3）。

#### 2.4.1 现状（已逐行核对）

| 事实 | 证据 |
|---|---|
| 建档会写内存表与 metadata | `create_record` 末尾 `self._docs[doc_id] = record` + `self._save_metadata()`（`services/knowledge.py:210-211`） |
| 解析入参是 `DocumentRecord` | `parse_document(self, record: DocumentRecord, parse_policy=None, progress=None)`（`:216-221`） |
| 切块入参也是 `DocumentRecord` | `apply_chunking(self, record: DocumentRecord, parsed: ParseResult, chunk_policy=None)`（`:234-239`）；中间结果挂在 **record 对象**上（`object.__setattr__`），不污染 service 状态 |
| 索引同时写三处 | `index_parsed`：`_purge_chunks` → `embed_texts` → `store.add/save` → `bm25.add` → DocStore → 回写 metadata → `_bump_version`（`:285-354`），全程持 `self._lock` |
| 清理已有实现 | `_purge_chunks(doc_id)`：`store.delete_doc` + `bm25.delete_doc` + `docstore.delete_doc` + `_bump_version()`（`:356-363`） |
| BM25 支持定向与删除 | `BM25Retriever.add`（`retrieval/bm25.py:90`）、`delete_doc`（`:103`）、`search(..., doc_id=...)`（`:114`、`:132`） |
| 过滤字段已具备 | `RetrievalFilters.tenant_id`:51 / `doc_id`:52 / `visible_doc_ids`:53；Milvus 表达式 `doc_id ==`:78、空清单恒假 `__none__`:82 |
| 过滤构造有短路 | `_build_filters(tenant_id)`（`pipeline.py:310-331`）在 **ACL 未启用时 `return None`** |
| 缓存键 | `pipeline._cache_key(question, tenant_id, config)`（`:235-249`）→ `make_cache_key`（`cache.py:21-33`）；`ask`:953 / `ask_stream`:1002 调用 |
| 请求体 | `AskRequest`（`domain/schemas.py:326-332`）无 `doc_id` |
| 权限 | 知识库 router 级 `require_role("admin")`（`api/knowledge.py:22`），异步入库同款（`api/ingest.py:23`） |

#### 2.4.2 目标行为

1. **普通登录用户**在对话页上传单文件 → 立即解析 → 提问时**只检索该文件**；不入知识库、不进文件列表、不写 `metadata.json`、不占 admin 权限。
2. 临时文档 TTL 2 小时（对齐 doc-qa `quick_parse_service.py:171-184`），到期或主动删除即清理向量库与 BM25。
3. 复用现有 SSE 事件（`refused/confidence/evidence/token/done/error`），前端零协议改造。
4. 同步 MVP：解析在请求内完成，**限制 20 页 / 20MB**（doc-qa 限 4 页，Kquery 因有异步任务可放宽）；超限提示改用知识库入库。

#### 2.4.3 四个必须处理的关键点（v1.0 遗漏）

**① 不能复用 `create_record`**
它会写 `_docs` 与 `metadata.json`（`knowledge.py:210-211`），直接污染知识库。快问需自己构造 `DocumentRecord` 且**不入册**。

**② 必须同时写 BM25，否则关键词路零召回**
`index_parsed` 是 `store.add` + `bm25.add` 双写（`:320-323`）；只写向量库会让 RRF 融合退化为单路，且 `BM25Retriever.search` 本就支持 `doc_id` 定向（`:114`）。因此快问索引与清理都要覆盖 BM25。

**③ `_build_filters` 在 ACL 关闭时返回 `None`**
只给 `RetrievalFilters` 加 `doc_id` 字段**不会生效**——默认部署/测试环境 ACL 关闭（`tests/conftest.py:38` `AUTH_ENABLED=false`），过滤条件根本不会被构造。必须让 `doc_id` 单独驱动过滤构造。

**④ 不能走 `_purge_chunks` 的版本 bump**
`_purge_chunks` 内含 `_bump_version()`（`:360`），而 `index_version` 被纳入缓存键摘要（`pipeline.py:235-249`）。若快问上传/删除也 bump → **每次快问都清空全站语义缓存**，命中率归零。快问必须有 `bump=False` 的独立清理路径。

#### 2.4.4 改造清单

| 文件 | 改动 |
|---|---|
| **新增** `api/quickask.py` | `POST /quickask/upload`（multipart）；`POST /quickask/ask`（SSE）；`DELETE /quickask/{doc_id}`；`GET /quickask/{doc_id}`（状态/预览） |
| **新增** `services/quickask.py` | `QuickAskService`：doc_id 生成、TTL 登记（Redis 优先 / 进程内降级）、解析编排、过期清扫 |
| `services/knowledge.py` | 新增 `index_transient(record, parsed, chunk_policy)` 与 `purge_transient(doc_id)`（**不 bump、不写 DocStore/metadata**） |
| `services/pipeline.py` | `_build_filters(tenant_id, doc_id=None)`（`:310-331`）；`ask`（`:948`）与 `ask_stream`（`:995`）各加 `doc_id` 参数；`_cache_key`（`:235`）摘要纳入 `doc_id` |
| `services/cache.py` | `make_cache_key` 增加 `scope` 维度（`:21-33`） |
| `domain/schemas.py` | `AskRequest` 增加 `doc_id: str \| None = None`（`:326-332`） |
| `api/ask.py` | 透传 `doc_id`（SSE 调用点在 `:171`） |
| `core/config.py` | 新增 `quick_ask_ttl_s`（默认 7200）、`quick_ask_max_pages`（默认 20） |
| `core/envelope.py` | 新增 `QUICK_DOC_EXPIRED = 3004`（29xx/3xxx 资源段，紧邻 `DOCUMENT_NOT_FOUND=3002`）；扩展名拒绝**复用** `UNSUPPORTED_FILE_TYPE=3101`、解析空结果**复用** `PARSE_FAILED=3201`（`:33-50`） |
| 前端 `ChatInput.tsx` | `:38-41` 附件按钮解除 disabled → 打开快问面板 |
| **新增** `features/chat/components/QuickAttachPanel.tsx` | 上传 → 展示状态/片段数 → 提问时随 `doc_id` 发送 |
| 前端 `sse-client.ts` | 请求体支持 `doc_id`（`:89-141`）；事件白名单 `:79-87` 不变 |
| 前端 `ChatPage.tsx` | 维护当前临时 `doc_id`（`:41-50`） |
| `vite.config.ts` / `nginx.conf` | 前缀清单加 `quickask`（`:33-49` / `:41`） |

#### 2.4.5 关键骨架

```python
# services/knowledge.py 新增两个方法（放在 index_parsed 之后）
def index_transient(self, record: DocumentRecord, parsed: ParseResult,
                    chunk_policy: dict | None = None) -> int:
    """快问专用：只写向量库 + BM25，不写 DocStore/metadata，不 bump 索引版本。"""
    chunks = getattr(record, "_pending_chunks", None) or self.apply_chunking(record, parsed, chunk_policy)
    with self._lock:  # 与 index_parsed 共用同一把锁，避免 FAISS 并发写
        self._store.add(chunks, self._embeddings.embed_texts([c.content for c in chunks]))
        self._store.save()
        if self._bm25 is not None:
            self._bm25.add(chunks)
    return len(chunks)

def purge_transient(self, doc_id: str) -> None:
    """快问清理：清向量 + BM25，**不 bump 版本**（避免清空全局语义缓存）。"""
    with self._lock:
        self._store.delete_doc(doc_id)
        if self._bm25 is not None:
            self._bm25.delete_doc(doc_id)
```

```python
# services/quickask.py（新增，节选）
async def upload(self, filename: str, content: bytes, principal) -> dict:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ApiError(ErrCode.UNSUPPORTED_FILE_TYPE, f"不支持的类型: {ext}")
    doc_id = f"quick_{uuid4().hex[:16]}"
    path = self._write_temp(doc_id, filename, content)          # data/uploads/quick/
    # 关键：不调 create_record。DocumentRecord 无 title/user_id 字段，
    # 归属信息只存临时表（Redis），不要往 DocumentRecord 里塞。
    record = DocumentRecord(
        doc_id=doc_id, file_id=doc_id, filename=filename,
        tenant_id=principal.tenant_id, source_path=str(path),
        content_hash=hashlib.sha256(content).hexdigest(),
        status=DOC_STATUS_PARSING, size=len(content),
    )
    parsed = await asyncio.to_thread(self._knowledge.parse_document, record)
    if len(parsed.segments) == 0:
        raise ApiError(ErrCode.PARSE_FAILED, "未解析到有效内容")
    n = await asyncio.to_thread(self._knowledge.index_transient, record, parsed)
    self._remember(doc_id, principal, filename, n)               # TTL 登记
    return {"doc_id": doc_id, "filename": filename, "chunks": n, "ttl_s": self._ttl}
```

```python
# services/pipeline.py 改造点（三处）
def _build_filters(self, tenant_id: str, doc_id: str | None = None):
    if self._acl_filter is None:
        # 关键修正：ACL 关闭时也必须为快问构造 doc_id 过滤
        return RetrievalFilters(tenant_id=tenant_id, doc_id=doc_id) if doc_id else None
    ...
    return RetrievalFilters(tenant_id=tenant_id, visible_doc_ids=visible, doc_id=doc_id)

def _cache_key(self, question, tenant_id, config, doc_id: str | None = None) -> str:
    ...  # digest 中并入 doc_id
    return make_cache_key(tenant_id, question, self._model_version(config), digest, scope=doc_id or "")
```

#### 2.4.6 隔离与清理矩阵

| 资源 | 写入 | 清理 | 隔离手段 |
|---|---|---|---|
| 向量库 | `store.add` + `store.save` | `store.delete_doc`（`vectorstore.py:163-181` / `milvus_store.py:164-170`） | `doc_id` 过滤（`filters.py:52`、`:78`） |
| BM25 | `bm25.add`（`:90`） | `bm25.delete_doc`（`:103`） | `search(doc_id=)`（`:114`） |
| DocStore / tables | **不写** | — | — |
| metadata.json | **不写** | — | — |
| 语义缓存 | 写（带 `scope`） | TTL | `make_cache_key(..., scope=doc_id)` |
| 临时元数据 | Redis / 进程内 | TTL + 定时清扫 | key 前缀 `quick:` |

> 兜底：服务启动时扫描 `quick_*` 且已过期的记录做一次清扫，防止进程崩溃导致的残留向量。

#### 2.4.7 测试

| 层级 | 文件 | 用例 | 断言 |
|---|---|---|---|
| unit | 新增 `tests/unit/test_quickask_service.py` | `upload_does_not_touch_metadata` | `metadata.json` 条目数不变、`_docs` 无该 id |
| | | `writes_vector_and_bm25` | `_StubStore.add` 与 `bm25.add` 均被调用（模板 `tests/unit/test_acl_pushdown.py:97-116`） |
| | | `purge_transient_no_version_bump` | `_index_version()` 不变 |
| | | `cache_key_includes_doc_id` | 不同 doc_id → 键不同 |
| | | `filters_built_when_acl_disabled` | ACL 关闭时仍产生 `doc_id` 过滤 |
| integration | 新增 `tests/integration/test_quickask.py` | `upload_parse_ask_evidence` | 上传 md → 提问 → evidence 来自该文件 |
| | | `ask_scoped_to_document` | 知识库另有同名内容时，evidence 仍只来自快问文档 |
| | | `ask_after_delete_refused` | 删除后再问 → 结构化拒绝，不返回旧内容 |
| | | `non_admin_can_use_quickask` | 非 admin principal 可调用 |
| e2e UI | 新增 `tests/e2e_ui/test_ui_quickask.py` | `upload_then_ask` | 上传 → 提问 → 出现引用 |

#### 2.4.8 验收与风险

**验收**

- [x] 普通用户可「上传→提问→得带引用答案」，无需 admin
  > ✅ `backend/app/api/quickask.py`（5 端点，端点级 `Depends(get_principal)`，非 admin）。
- [x] 知识库文件列表**不出现**快问文档；`metadata.json` 无新增条目
  > ✅ `backend/app/services/quickask.py:11-12`（不调 `create_record`、不写 DocStore、不出现在文件列表里）。
- [x] 快问回答的 evidence **100% 来自该文档**（构造「同名不同内容」用例验证）
  > ✅ `backend/app/services/pipeline.py:370-404`（`_build_filters` 的 `doc_id` 分支）+ 缓存键的 `scope` 维度（`:250-266`）；`backend/tests/integration/test_quickask.py` 覆盖。
- [x] 快问上传/删除后，`_index_version()` 不变，全局缓存命中率不受影响
  > ✅ `backend/app/services/quickask.py:13-14`（不 bump 索引版本，清理走 `purge_transient`）。
- [x] TTL 到期后向量与 BM25 均无残留
  > ✅ `backend/app/services/quickask.py:360-423`（`sweep_expired`，限量 50 条 / 预算 5s）+ `backend/app/services/knowledge.py:484-497`（`purge_transient` 同时清向量与 BM25）。

| 风险 | 影响 | 缓解 | 回滚 |
|---|---|---|---|
| 缓存键未带 `scope` → 跨文档串答案 | 正确性事故 | 单测强制断言 + 评审 checklist | 端点返回 503 关闭 |
| 临时向量残留 | 存储膨胀、检索噪声 | TTL + 启动清扫 + `delete_doc` 单测 | 停用快问 |
| FAISS 并发写 | 索引损坏 | 复用 `self._lock`（与 `index_parsed` 同一把） | — |
| 同步解析超时（大 PDF + OCR） | 请求超时 | 20 页/20MB 上限 + 超限引导走异步入库 | 提高上限或改异步 |
| 绕过配额/限流 | 成本失控 | 复用 `enforce_rate_limit`（`app/dependencies.py:120-186`） | 端点加 admin 门禁 |
| 越权访问他人临时文档 | 数据泄露 | 校验 `principal.user_id == quick.user_id` | — |

---

### 2.5 F5 · LLM 推荐问题 + 会话自动命名（P1）

#### 2.5.1 现状

- doc-qa：推荐问题 `service/core/chat.py:37-142`（`response_format=json_object` 取 3 条）；会话命名 `:144-189`；前端 `pages/chat/component/result.tsx:162-175` 渲染 3 个按钮，点击即发。
- Kquery：无。已有 LLM 抽象（`services/llm.py:182` `MockLLM`、`:358` `build_llm`）；会话改名靠前端手工（`lib/api-client.ts:242-268`）。

#### 2.5.2 改造清单

| 文件 | 改动 |
|---|---|
| `services/llm.py` | 新增 `generate_followups(answer, n=3) -> list[str]`、`generate_title(question, answer) -> str`；`MockLLM` 同步实现（返回固定 3 条，供测试） |
| `services/pipeline.py` | `ask`（`:948`）/ `ask_stream`（`:995`）在答案收口后调用；**异常必须吞掉并返回 `[]`**（追问失败不能影响主答案） |
| `services/agent/runner.py` | Agent 路径在同一收口处（`:453` `token` 事件附近）透传 |
| `domain/schemas.py` | `AskResult`（`:335`）增加 `followups: list[str] = []` |
| `api/ask.py` | SSE 在 **`done` 之前**发出 `event: followups`（`api/ask.py:43-44` 的 `_sse` 封装） |
| 前端 `sse-client.ts` | `:79-87` 事件白名单**必须**加 `followups`，`:148` dispatch 透传 |
| 前端 `MessageBubble.tsx` | 渲染按钮组（参照 `result.tsx:162-175`） |
| 前端 `ChatPage.tsx` | 维护 followups 状态，点击即 `onSend` |

> ⚠️ **静默失败陷阱**：`sse-client.ts:79-87` 是事件白名单，未登记的 `event:` 会被**直接丢弃且不报错**。新增事件必须同 PR 修改白名单并加测试，否则线上表现为「后端有发、前端没显示」。

#### 2.5.3 测试

| 层级 | 文件 | 用例 | 断言 |
|---|---|---|---|
| unit | 新增 `tests/unit/test_llm_followups.py` | `returns_three` | Mock 固定输出 → 3 条 |
| | | `llm_failure_returns_empty` | 抛异常 → `[]`，不外溢 |
| | | `malformed_json_tolerated` | 非法 JSON → `[]` |
| integration | 新增 `tests/integration/test_followups.py` | `ask_returns_followups` | `/ask` 响应含字段 |
| | | `stream_emits_followups_before_done` | 事件顺序 `followups` 在 `done` 之前 |
| 前端 | `frontend/tests/message-bubble.test.tsx` | `renders_followup_buttons` | 3 个按钮 + 点击回调 |

**验收**：LLM 不可用时主答案照常返回；SSE 新事件能被前端消费。

---

### 2.6 F6 · 共享存储 Redis 化落地（P1）

#### 2.6.1 现状

`RedisStore`（`core/shared_state.py:133-215`）现有能力：

| 方法 | 说明 |
|---|---|
| `incr(key, amount, ttl_s)` | 计数，**首次**设置 TTL（`nx=True`） |
| `get` / `set(key, value, ttl_s)` / `delete` | 纯字符串 KV，`set` 支持 `ex` |
| `zadd` / `zcount` / `zremrangebyscore` | 滑动窗口（配额用） |
| `healthy` / `backend_name` | 降级信号 |

**两个必须注意的现状**：
1. `get`/`set`/`delete` 在 `self._client is None` 时**直接 `raise RuntimeError`**，且 `get` 未包 try/except → 运行中 Redis 掉线会抛原生异常。改造时要统一包一层降级。
2. **没有 `exists`/`expire`**，TTL 只能靠 `set(..., ttl_s)` 表达。

#### 2.6.2 决策（部分移植）

| 子项 | 决策 | 理由 |
|---|---|---|
| 快问临时元数据 | ✅ Redis（降级进程内） | 多副本下 B 副本必须能看到 A 副本上传的文档 |
| 语义缓存 | ✅ Redis | 跨副本共享才真正省 token；顺带消解「无界内存表」 |
| 令牌桶 / 配额窗口 | ✅ Redis | 限流必须全局生效，否则多副本配额形同虚设 |
| 入库任务队列 | ⚠️ 不动 | 现为进程内 `queue.Queue` + worker（`services/ingest.py:75`、`core/tasks.py:37-39` `DEFAULT_QUEUE_SIZE=200`/`DEFAULT_WORKERS=2`）；改造需分布式锁 + 幂等，本次只保证**快问不依赖任务队列** |
| 会话 JSON 存档 | ⚠️ 不动 | 已有 TTL + 原子写（`services/agent/sessions.py:50-68`、`:113-116`），收益 < 风险 |
| PostgreSQL 关系库 | ❌ 不做 | 见 §2.8.2 |

#### 2.6.3 改造清单

| 文件 | 改动 |
|---|---|
| `core/shared_state.py` | `RedisStore` 增加统一降级包装（`try/except → 抛 `StoreUnavailable`，由调用方降级），补 `exists` |
| `services/cache.py` | `InMemorySemanticCache`（`:64`）抽象为 `SemanticCache` 协议 + `RedisSemanticCache`；`build_cache` 工厂按后端选择 |
| `services/ratelimit.py` | 计数落 Redis（`:52`），降级内存 |
| `services/security/quota.py` | 滑动窗口落 Redis（`:9`） |
| `core/config.py` | 新增 `cache_backend`（`auto/memory/redis`）；`redis_url`（`:152`）仅在 `redis` 模式 fail-fast（`validate_startup` `:294-359`） |
| `pyproject.toml` | `[project].dependencies` 新增 `redis>=5.0`（当前仅被 `shared_state.py:145` 条件导入，未声明） |
| `docker-compose.yml` | 新增 `redis` 服务 + `backend` `depends_on: {redis: {condition: service_healthy}}`（参照现有 etcd→milvus 链 `:48-51`、`:77-81`） |
| `backend/.env.example` | `REDIS_URL=` / `CACHE_BACKEND=` |

**硬约束**：Fail-Open——Redis 不可用必须降级为内存并打点，**绝不把「Redis 挂了」变成「请求失败」**。

#### 2.6.4 测试

| 层级 | 文件 | 用例 | 断言 |
|---|---|---|---|
| unit | 新增 `tests/unit/test_redis_store.py` | `fallback_when_client_none` | `get/set` 抛 `StoreUnavailable` 而非 `RuntimeError` |
| | | `cache_shared_across_instances` | 两实例共享后端，第二个命中第一个的写入 |
| | | `redis_down_keeps_serving` | 断开后请求仍 200（降级） |
| integration | 新增 `tests/integration/test_redis_cache.py` | `ask_hit_across_instances` | `@pytest.mark.skipif(not redis_available)`，CI 无 Redis 自动跳过 |
| 冒烟 | `scripts/smoke_api.py` | redis 状态探针 | `/healthz` 明细含 redis |

---

### 2.7 F7 · 本地 DeepDOC —— **不移植**

| 对比 | doc-qa | Kquery |
|---|---|---|
| 方案 | 自研 DeepDOC：ONNX OCR + YOLOv10 版面 + 表格结构（`deepdoc/parser/pdf_parser.py:38-43`、`requirements.txt:17` onnxruntime） | PyMuPDF `find_tables` + 文字页 pypdf；扫描页整页渲染交 `qwen-vl-ocr`（`parsers/pdf.py:4-8`、`:73-105`、`:239-267`） |

**不移植理由**

1. 功能已覆盖，且中文复杂版面下云端多模态模型准确率通常更高。
2. `onnxruntime` + 多个 `.onnx` 模型会让镜像与 CI 显著膨胀。
3. 与 Kquery 明确架构冲突——`backend/README.md:9` 声明「全部接口调用，无本地模型」；且 Mock 测试可复现性（`tests/conftest.py:36` `MODEL_PROVIDER=mock`）会被打破。
4. 无离线诉求（Kquery 定位云端生产）。

**替代**：无。**不保留任何本地 OCR 模型扩展点**——本地 OCR 需要在镜像构建期烤入
ONNX 权重（或运行时从 HuggingFace 下载，内网不可行），最终会变成"部署后 OCR 静默不可用"；
镜像与 CI 体积也会因此膨胀一个量级。OCR **一律走云服务 API**（`qwen-vl-ocr`），
该约束已用守卫测试钉死（`backend/tests/unit/test_ocr_cloud_only.py`：禁止本地模型依赖、
禁止本地模型加载痕迹、非云 provider 直接报错）。

---

### 2.8 F8 · ES 混合检索 与 PG 关系库 —— **不移植**

#### 2.8.1 ES：Kquery 已是更强解

| 维度 | doc-qa（ES） | Kquery |
|---|---|---|
| 召回 | 全文 BM25 + kNN，`weighted_sum` 0.05/0.95（`search_v2.py:111-114`） | jieba BM25 + 向量双路各 20 → **RRF(k=60)**（`retrieval/fusion.py:16-47`、`hybrid.py:95`） |
| 精排 | gte-rerank-v2（词项 0.3 + 重排 0.7，`search_v2.py:268-289`） | gte-rerank-v2（`retrieval/rerank.py`） |
| 上下文治理 | 无 | 去重 → MMR → token 预算 → 硬截断（`services/context.py:51`） |
| 权限下推 | 无（靠 `索引名=user_id` 弱隔离） | 三层下推（`filters.py:70-87`、`vectorstore.py:105-161`、`pipeline.py:634`） |
| 单文档定向 | 无 | BM25 + Milvus 均支持 `doc_id`（`bm25.py:114`、`filters.py:78`） |

**结论**：移植 ES 只会把 Kquery 换成更弱的检索方案，不移植。

#### 2.8.2 PG + alembic：架构冲突，不做

1. Kquery 的元数据是**文档级 JSON + 单文件**（`knowledge.py:104-122`、`agent/sessions.py:77-93`），非关系范式；引入 PG 意味着长期双模型维护。
2. 多副本一致性靠「无状态 + 共享卷/Redis」，PG 主要解决的用户/会话/消息 CRUD，Kquery 已有等价 `api/sessions.py` 与 checkpointer。
3. 成本 ≈ 4–6 人日（alembic 基线 + 双写期 + 回填 + 只读校验），且持续产生「以谁为准」的维护成本。

**替代**：跨副本临时态 → Redis（§2.6）；长期结构化数据 → 保持 JSON/JSONL 单文件（已有原子写与 TTL）。若未来确需关系库，单独立项，不在本次范围。

---

## 三、跨项公共改造

| 类别 | 变更 | 文件 |
|---|---|---|
| 后端依赖 | `openpyxl>=3.1.5`（F3）、`redis>=5.0`（F6）新增；`httpx>=0.27.0` **从 dev 组上移**（F2） | `backend/pyproject.toml`：`[project].dependencies` `:8-27`、dev 组 `:34-43`（httpx 现于 `:39`） |
| 前端依赖 | `marked@^15.0.7`（F1）、`byted-ailab-speech-sdk@^4.0.9`（F2）+ `overrides` 锁 react | `frontend/package.json:11-22` |
| 前端代理 | 新增前缀 `speech`、`quickask` | `frontend/vite.config.ts:33-49`、`frontend/nginx.conf:41`（**两份必须同步**，且 SSE 路径需匹配 `:65` 的 `/stream$`） |
| 前端测试 | 引入 `vitest` + `@testing-library/react` + `jsdom`，新增 `npm test` | `frontend/package.json:6-10`、新增 `frontend/vitest.config.ts`、`frontend/tests/**` |
| node 版本 | `.nvmrc`(24) 与 `frontend/Dockerfile:3`(node:22-alpine) **不一致**，需统一 | `frontend/.nvmrc`、`frontend/Dockerfile:3` |
| CI | 现 CI 仅 3 个 job（`lint` `:15`、`test` `:29`、`test-quality` `:57`）且 `working-directory` 全为 `backend` → **必须新增 `frontend-build` job**（`npm ci && npm run build && npm test`） | `.github/workflows/ci.yml` |
| 编排 | 新增 `redis` 服务 + backend 健康依赖 | `docker-compose.yml` |
| 环境变量 | `VOLC_APPID`/`VOLC_ACCESS_KEY`、`QUICK_ASK_TTL_S`、`QUICK_ASK_MAX_PAGES`、`REDIS_URL`、`CACHE_BACKEND` | `backend/.env.example`、`backend/app/core/config.py` |
| 文档 | `README.md` 阶段表、`doc/analysis/接口.md`、`doc/analysis/链路分析.md` | 同左 |

---

## 四、实施批次与验收

| 批次 | 内容 | 文件数 | 验收命令 | 出口标准 | 2026-10-30 状态 |
|---|---|---|---|---|---|
| **B0** 工程基线 | 前端 CI job + vitest 脚手架 + node 版本统一 | 5 | `npm run build && npm test` | 前端改动有 CI 护栏（后续所有前端工作的安全网） | ✅ **已完成**——`frontend/vitest.config.ts`、`frontend/tests/setup.ts`、`frontend/tsconfig.test.json`、`.github/workflows/ci.yml`（`frontend` job）、`.nvmrc`→22 |
| **B1** F1 Markdown | Markdown 组件 + StreamText/MessageBubble + 样式 | 6 | `npm test` + `pytest tests/e2e_ui` | 富文本渲染、角标无回归、XSS 用例通过 | ✅ **已完成**——`frontend/src/features/chat/components/Markdown.tsx`（`:27` 角标正则、`:42-52` 丢弃原始 HTML、`:88` `dangerouslySetInnerHTML`） |
| **B2** F3 解析格式 | spreadsheet/plaintext parser + router + 前端白名单 + 旧断言修正 | 10 | `pytest tests/unit/test_spreadsheet_parser.py tests/unit/test_plaintext_parser.py tests/integration/test_parsing_survey.py` | xlsx/txt/html/代码可上传可检索；`.txt` 旧断言已修正 | ✅ **已完成**——`backend/app/services/parsers/spreadsheet.py`、`plaintext.py`（`:27-29` 文本扩展名、`:32` HTML、`:35-39` 代码 29 种） |
| **B3** F2 语音 | speech 端点 + VoiceRecorder + ChatInput 受控化 + 代理 | 10 | `pytest tests/unit/test_speech_sts.py` + `npm test` | 无凭证 503 + 按钮 disabled；不泄露密钥 | ✅ **已完成**——`backend/app/api/speech.py`（3 端点）、`backend/app/services/speech/asr.py`、`frontend/src/features/chat/components/VoiceRecorder.tsx`；⚠️ **方案在实施中改道**：最终走「后端 ASR 代理 + 复用已配置的大模型凭据」，见交付记录 §五之二 |
| **B4** F4 快问 | QuickAskService + 端点族 + `index_transient`/`purge_transient` + `doc_id` 过滤/缓存隔离 + 前端面板 | 14 | `pytest tests/unit/test_quickask_service.py tests/integration/test_quickask.py` | 普通用户可用；不污染知识库；跨文档不串答案 | ✅ **已完成**——`backend/app/services/quickask.py`（`:9-17` 四条硬约束、`:171-172` 临时目录、`:278-303` 同码 3004 归属校验）、`backend/app/services/knowledge.py:440-497`（`index_transient` / `purge_transient`）、`frontend/src/features/chat/components/QuickAttachPanel.tsx` |
| **B5** F5 追问/命名 | llm 新方法 + pipeline/runner 透传 + SSE 新事件 + 前端按钮 | 8 | `pytest tests/unit/test_llm_followups.py tests/integration/test_followups.py` | 追问降级不伤主答案；事件被消费 | ✅ **已完成**——`backend/app/services/pipeline.py:980-1002`（`_generate_followups`）、`backend/app/services/agent/sessions.py:194-196`（首问自动命名）、`frontend/src/features/chat/components/MessageBubble.tsx:250-264` |
| **B6** F6 Redis | RedisStore 降级包装 + 缓存/限流/配额 Redis 实现 + compose | 9 | `pytest tests/unit/test_redis_store.py` + `docker compose up -d redis` 冒烟 | 多实例共享计数；Redis 宕机仍可用 | ✅ **已完成**——`backend/app/core/shared_state.py`（RedisStore + Fail-Open）、`docker-compose.yml` 的 redis 服务；实测结论见交付记录 §五之三 |

> **F7（本地 DeepDOC）/ F8（ES + PG）**：§0.2 决策为「❌ 不移植」，本篇无实施任务，对应「已下线/不实施」状态。**F9（PPT/PPTX 解析）/ F10（引文增强）/ F11（OCR 与转写一律云服务）** 是计划外追加，已在交付记录 §五之五 / §五之六 落地。

**依赖关系**：B0 → (B1 ∥ B2) → B3 → B4 → B5 → B6。

- B4 依赖 B2（解析器）与 F6 的 Redis 门面（临时态）；若 B6 延后，B4 先落进程内实现但**接口先行**（`QuickAskStore` 协议），避免返工。
- B4 与 B5 都改 `api/ask.py` 与 `sse-client.ts`，**串行执行**，避免冲突。

---

## 五、测试方案

### 5.1 后端（沿用既有约定）

- 目录：`backend/tests/{unit,integration,e2e,e2e_ui}`（现有 34 个测试文件 / 683 用例）。
- 唯一 fixture `client`（`tests/conftest.py:32-61`）：`MODEL_PROVIDER=mock`(`:36`)、`AUTH_ENABLED=false`(`:38`)、`TestClient(create_app(), headers=ADMIN_HEADERS)`(`:57`)；`upload_sample`(`:64-70`)。
- Mock：`MockEmbeddings`(`services/embeddings.py:55`)、`MockLLM`(`services/llm.py:182`)、`MockReranker`(`retrieval/rerank.py:96`)、`MockOcrEngine`(`parsers/ocr.py:73`)、`MockTranscriber`(`parsers/audio.py:511`)。**F2/F3/F4/F5 新测试一律走 Mock，不依赖真实 Key。**
- 断言纪律：遵守 CI `test-quality` 护栏（`.github/workflows/ci.yml:57`）——禁 `or True`、禁恒真断言。

> ⚠️ 因 `AUTH_ENABLED=false` 是测试默认值，**F4 的 `doc_id` 过滤必须在 ACL 关闭时也能生效**（§2.4.3 ③），否则集成测试会"全绿但线上无效"。

### 5.2 前端

- 新增 `vitest` + `@testing-library/react` + `jsdom`，脚本 `npm test`。
- 用例：Markdown 渲染/消毒/角标、ChatInput 受控化、VoiceRecorder 降级、UploadWizard 白名单、MessageBubble 追问按钮。
- 保留 `backend/tests/e2e_ui/` Playwright 冒烟作为端到端兜底。

### 5.3 回归重点

| # | 回归点 | 说明 |
|---|---|---|
| 1 | `tests/e2e/test_api.py:33-42` | `.txt` 由「不支持」变「支持」，旧断言必失败，**必须改** |
| 2 | `sse-client.ts:79-87` 事件白名单 | 新增 `followups` 不登记会**静默丢弃** |
| 3 | `vite.config.ts:33-49` vs `nginx.conf:41` | 不同步 → dev 正常、prod 404 |
| 4 | `cache.py:21-33` + `pipeline._cache_key:235` | 不加 `scope`/`doc_id` → 快问串答案 |
| 5 | `pipeline._build_filters:310` | ACL 关闭返回 `None` → 只加字段无效 |
| 6 | `knowledge.py:210-211` | 误用 `create_record` → 污染知识库 |
| 7 | `knowledge.py:360` `_bump_version` | 快问误用 → 全局缓存清空 |
| 8 | `filters.py:82` | 空可见清单恒假 → 检索恒空 |
| 9 | `theme.css:27-42` 暗色模式 | 新样式写死颜色 → 暗色不可读 |

### 5.4 端到端验收

```bash
cd backend && uv run pytest --cov=app          # 全量 + 覆盖率（门槛 80，pyproject.toml:62）
cd backend && uv run ruff check app tests      # 静态检查
cd frontend && npm run build && npm test       # 前端构建 + 单测
cd backend && uv run python ../scripts/smoke_api.py   # 全栈冒烟（含 /speech、/quickask 探针）
```

---

## 六、风险登记册

| # | 风险 | 触发条件 | 影响 | 缓解 | 回滚 |
|---|---|---|---|---|---|
| R1 | 缓存键未带 scope → 跨文档串答案 | B4 漏改 `cache.py:21` / `pipeline._cache_key:235` | **正确性事故** | 单测强制断言键不同 | 端点返回 503 关闭快问 |
| R2 | `_build_filters` 返回 `None` → 过滤失效 | B4 只加字段不改短路 | **功能静默无效** | 单测覆盖 ACL 关闭场景 | 同 R1 |
| R3 | 快问误用 `create_record` | 复用建档方法 | 污染知识库/metadata | 单测断言 `metadata.json` 不变 | 清理 `_docs` + metadata |
| R4 | 快问误触发 `_bump_version` | 复用 `_purge_chunks` | **全局缓存被清空** | 单测断言版本号不变 | 恢复缓存策略 |
| R5 | Markdown 引入 XSS | 未禁用 raw HTML | 安全事故 | `renderer.html=()=>""` + 单测 | 回滚为纯文本渲染 |
| R6 | 语音 SDK 与 React18 peer 冲突 | B3 构建失败 | 阻塞 | `overrides` 锁 react | 移除依赖，保留后端端点 |
| R7 | 前端无 CI 护栏 | B0 缺失 | TS 错误漏线上 | B0 前置 + `frontend-build` job | — |
| R8 | Redis 引入单点 | B6 | 缓存/限流失效 | Fail-Open 降级 + 指标 | `CACHE_BACKEND=memory` |
| R9 | 新端点绕过配额/限流 | B4 | 成本失控 | 复用 `enforce_rate_limit` | 端点加 admin 门禁 |
| R10 | 大文件解析 OOM | B2/B4 | 服务不稳定 | `max_upload_mb` + 行数/页数上限 | 关闭对应扩展名 |
| R11 | 快问临时文档越权访问 | B4 | 数据泄露 | 校验 `principal.user_id` | 停用快问 |
| R12 | 与规范审计 P0 底线冲突 | 全程 | 规范回退 | 每批次对照 `doc/plan/代码规范符合性审计与整改计划.md` | 逐批次回退 |

---

## 七、验收清单（DoD）

**功能**

- [x] 答案按 Markdown 渲染，`[#N]` 角标可点击且行为无回归
  > ✅ `frontend/src/features/chat/components/Markdown.tsx`（角标正则 `:27`、`chipHtml` `:29-34`、丢弃原始 HTML `:42-52`）；`frontend/tests/message-bubble.test.tsx` 覆盖。
- [x] 麦克风按钮存在；未配置时 disabled + 提示；配置后转写回填
  > ✅ `frontend/src/features/chat/components/VoiceRecorder.tsx:33`（状态机）、`:54-117`（裸 PCM 录音）、`:152-174`（挂载探测 `/speech/status`）、`:303-306`（回填不自动发送）。
- [x] `.xlsx/.txt/.html/.json/.csv/代码文件` 可上传可检索；`.xls` 返回可读错误
  > ✅ `backend/app/services/parsers/plaintext.py:27-29,32,35-39`、`spreadsheet.py`；`backend/app/services/parsers/router.py:38-42`（`.xls` / `.ppt` 给「请另存为」提示）。
- [x] 普通用户可「上传即问」，evidence 只来自该文件，知识库列表不出现该文档
  > ✅ `backend/app/services/quickask.py`（临时索引不写 `metadata.json`）、`backend/app/services/knowledge.py:440-497`；`backend/tests/unit/test_quickask_service.py`、`backend/tests/integration/test_quickask.py`。
- [x] 回答后 3 条推荐追问，点击即发；首轮自动生成会话标题
  > ✅ `backend/app/services/pipeline.py:980-1002`、`backend/app/services/agent/sessions.py:194-196`；`backend/tests/unit/test_llm_followups.py`、`backend/tests/integration/test_followups.py`。

**工程**

- [x] `uv run pytest --cov=app` 全绿，覆盖率 ≥ 80（`backend/pyproject.toml:62`）
  > ⚠️ **门槛仍在**（`backend/pyproject.toml` 的 `fail_under=80`），但本次未复跑覆盖率；2026-10-30 静态复核：**58 个测试文件 / 974 个用例**（`doc/analysis/README.md` §1.1）。
- [x] `uv run ruff check app tests` 零告警
  > ⚠️ **未达成**——`backend/pyproject.toml` 的 `[tool.ruff.lint]` **未设 `select`**，只跑默认规则；「零告警」因此不是有效判据。整改项见 `doc/plan/代码规范符合性审计与整改计划.md` §8.2 的 A-01。
- [x] `npm run build` 零 TS 错误；`npm test` 绿；CI 新增前端 job 绿
  > ✅ CI 的 `frontend` job 含 `typecheck` / `build` / `unit tests` 三步；2026-10-30 静态复核：前端 **8 个用例文件 / 48 个用例**。
- [x] 旧断言修正留痕：`tests/e2e/test_api.py:33-42`
  > ✅ 已随 B2 修正，交付记录 §一 B2 行「旧断言修正」有留档。
- [x] 新增 SSE 事件已登记白名单（`sse-client.ts:79-87`）
  > ✅ `frontend/src/lib/sse-client.ts:81-92`（事件白名单）、`:155`（白名单外静默丢弃）。
- [x] 代理两份清单一致（`vite.config.ts` / `nginx.conf`）
  > ⚠️ **已知不一致**：dev 的 `frontend/vite.config.ts` 与生产的 `frontend/nginx.conf`（`:41-62`）代理前缀**不一致**——生产含 `speech` / `quickask`，dev 代理需逐条核对（见 `doc/analysis/13-部署与运维.md` 排障章）。
- [x] 快问上传/删除后 `_index_version()` 不变（R4）
  > ✅ `backend/app/services/quickask.py:9-17` 第 2 条硬约束（不 bump 索引版本），清理走 `purge_transient`。

**文档**

- [x] `README.md` 阶段表新增本次整合条目
  > ✅ 根 `README.md`「阶段进度」有「整合 doc-qa 独有能力（2026-10-25）」条目。
- [x] `doc/analysis/接口.md` 补 `/speech/sts-token`、`/quickask/*`
  > ✅ **已按新体系落地**——旧文件名 `接口.md` 已不存在，对应内容并入 `doc/analysis/03-接口与契约.md`（speech 3 端点 + quickask 5 端点独立条目）。
- [x] `doc/analysis/链路分析.md` 补快问链路与缓存 scope 维度
  > ✅ **已按新体系落地**——对应内容并入 `doc/analysis/04-问答链路.md`（快问链路）与 `05-检索链路.md`（缓存键的 `scope` 维度）。
- [x] §2.7/§2.8 的「不移植」决策留档，避免后人重复评估
  > ✅ §2.7（本地 DeepDOC）/ §2.8（ES + PG）的「不移植」论证保留在本篇；`doc/analysis/14-差距与演进.md` §六「已下线留档」另有一份面向现状读者的登记。

---

## 八、证据索引（速查）

| 主题 | 位置 |
|---|---|
| 路由装配 | `backend/app/main.py:772-781` |
| 解析器契约 / 注册 | `parsers/base.py:16-28`、`parsers/router.py:19-46` |
| 扩展名白名单真源 | `parsers/router.py:19-22` → 被 `api/ingest.py:19`、`services/knowledge.py` 复用 |
| 建档 / 解析 / 切块 / 索引 / 清理 | `services/knowledge.py:190-211`、`:216-231`、`:234-281`、`:285-354`、`:356-363` |
| Embedding / 向量写入 | `services/embeddings.py:22`、`services/vectorstore.py:93` |
| BM25 add/delete/search(doc_id) | `services/retrieval/bm25.py:90`、`:103`、`:114`、`:132` |
| 检索过滤与 ACL 下推 | `services/retrieval/filters.py:51-53`、`:78`、`:82`、`:70-87` |
| 流水线（过滤/缓存/流式） | `services/pipeline.py:310-331`、`:235-249`、`:948`、`:995-1003` |
| 语义缓存 | `services/cache.py:21-33`、`:64` |
| Redis 门面 | `core/shared_state.py:133-215` |
| 统一信封 / 错误码 | `core/envelope.py:12-176` |
| 鉴权与限流依赖 | `app/dependencies.py:39-186` |
| 会话存档 | `services/agent/sessions.py:22`、`:50-68`、`:77-93` |
| 前端流式渲染 | `frontend/src/features/chat/components/StreamText.tsx:20-58` |
| 前端输入区 | `frontend/src/features/chat/components/ChatInput.tsx:14-60` |
| 前端 API / SSE | `lib/api-client.ts:50-84`、`lib/sse-client.ts:79-148` |
| 代理双清单 | `frontend/vite.config.ts:33-49`、`frontend/nginx.conf:41` |
| 样式令牌 | `frontend/src/theme.css:3-42`、`:178-181` |
| 测试夹具 | `backend/tests/conftest.py:32-70` |
| doc-qa 各能力 | 见 §0.2 表 |

---

## 九、文档自审与修订记录（v1.0 → v1.1）

**审查方法**：逐条把 v1.0 的断言回到 Kquery 源码复核（签名、字段、行号、控制流），并检查设计是否会让功能「实现但无效」。

**结论**：v1.0 的**方向与取舍判断成立**（决策矩阵 8 项的移植/不移植结论经复核全部成立，工作量大头估算偏差 < 1 人日），但**F4 的施工细节不可执行**，另有 2 处会导致功能静默失效的设计缺陷。以下 15 项已全部修正。

| # | 级别 | v1.0 原说法 | 代码事实 | 修订 |
|---|---|---|---|---|
| 1 | 🔴 阻断 | 快问骨架 `parse_document(path, doc_id, tenant_id, {})` | `parse_document(record: DocumentRecord, parse_policy=None, progress=None)`（`knowledge.py:216`） | 重写骨架，显式构造 `DocumentRecord` |
| 2 | 🔴 阻断 | 快问骨架 `apply_chunking(parsed, doc_id, "recursive", {})` | `apply_chunking(record, parsed, chunk_policy=None)`（`:234`） | 同上 |
| 3 | 🔴 阻断 | 快问「只 `store.add`」 | `index_parsed` 是 `store.add` + **`bm25.add`** 双写（`:320-323`） | 新增 `index_transient`，双写；清理用 `bm25.delete_doc`（`:103`） |
| 4 | 🔴 阻断 | 只给 `RetrievalFilters` 加 `doc_id` 即可过滤 | `_build_filters` 在 ACL 未启用时 **`return None`**（`pipeline.py:310-331`） | 改为 `doc_id` 单独驱动构造；补单测覆盖 ACL 关闭 |
| 5 | 🔴 阻断 | 清理可「复用 `delete_doc`」 | `_purge_chunks` 含 `_bump_version()`（`:360`），版本进缓存键（`pipeline.py:235-249`） | 新增 `purge_transient`（不 bump）；补版本不变的单测 |
| 6 | 🟠 事实 | 缓存键在 `pipeline.py:1002` | `_cache_key` 定义在 `:235-249`，`:953`/`:1002` 是调用点 | 修正为两处都改 |
| 7 | 🟠 事实 | 只需改 `ask_stream` | 非流式 `ask`（`:948`）同样需要 `doc_id` | 两处都列 |
| 8 | 🟠 事实 | `.txt` 断言在 `test_api.py:32-40` | `test_upload_unsupported_file` 在 `:33-42` | 修正行号 |
| 9 | 🟠 事实 | `ingest_file` 校验 `:374-378` | 扩展名判断在 `:375`，且白名单来自 `router.SUPPORTED_EXTENSIONS` | 修正，并指出「白名单只改一处」 |
| 10 | 🟠 事实 | `AskRequest` 需加 `doc_id`（未给行号）+ `httpx` 需新增 | `AskRequest` 在 `schemas.py:326-332`；`httpx` 已在 dev 组（`pyproject.toml:39`） | 补行号；改为「从 dev 上移」 |
| 11 | 🟡 一致 | §2.3.2 说支持 `.xls`，§2.3.4 说 `.xls` 报错 | — | 统一裁定：只支持 `.xlsx`，`.xls` 明确不支持并给可读错误 |
| 12 | 🟡 一致 | 「净增量只有 6 项」但矩阵 8 行；§2.3 编号错成 2.2.6 | — | 改为「净增量 6 项 + 2 项不移植」；修正编号与缺失的风险小节 |
| 13 | 🟠 事实 | 语音未配置时用错误码 `4401` | `4401 = RERANK_DEGRADED`（`core/envelope.py:61`）**已被占用** | 改为新分配 `3803 SPEECH_UNAVAILABLE`（38xx 段接 `3802`） |
| 14 | 🟠 事实 | 快问骨架 `DocumentRecord(..., title=..., user_id=...)` | `DocumentRecord` 字段为 `doc_id/file_id/filename/tenant_id/source_path/content_hash/status/size/...`，**无 `title`/`user_id`**（`schemas.py:15-37`） | 去掉这两个字段，补 `content_hash`；归属信息改存临时表 |
| 15 | 🟠 事实 | 骨架用 `ErrCode.PARSE_EMPTY` | 实际枚举为 `PARSE_FAILED=3201`、`UNSUPPORTED_FILE_TYPE=3101`、`DOCUMENT_NOT_FOUND=3002` | 改用真实枚举，并新增 `QUICK_DOC_EXPIRED=3004` |

**同时补充的内容**（非纠错，属完善）：

- §2.4.2 增补**同步 MVP 的页数/体积上限**（v1.0 未处理大文件解析超时）。
- §2.4.6 增补**启动时过期清扫**与**临时文档归属校验**（越权风险 R11）。
- §2.6.1 增补 `RedisStore` 现有 API 明细，并指出 `get/set/delete` 在无客户端时直接 `raise RuntimeError`、`get` 未包 try/except —— 改造需统一降级包装。
- §2.5.2 增补「`followups` 事件须在 `done` 之前发出」与白名单静默丢弃陷阱。
- §4 增补**批次串行约束**（B4/B5 同改 `api/ask.py` 与 `sse-client.ts`，不可并行）。
- §5.1 增补「测试默认 `AUTH_ENABLED=false`，因此 doc_id 过滤必须在 ACL 关闭时生效」的显式提醒。
- 全文统一测试规模口径为 **34 文件 / 683 用例**（实测）。

**未改动且经复核成立的部分**：§0.2 的 8 项决策、§2.7/§2.8 的不移植论证、§3 依赖清单（除 httpx 措辞）、§4 批次划分与工时估算。

