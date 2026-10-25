# 实施记录 · 整合 doc-qa 独有功能到 Kquery

> 依据：`doc/plan/整合doc-qa独有功能到Kquery-分析与实施计划.md`（v1.1）
> 实施日期：2026-10-25
> 状态：**B0 ~ B6 全部完成**，后端 794 用例 / 前端 26 用例全绿，覆盖率 84.80%（门槛 80%）

---

## 一、批次交付情况

| 批次 | 内容 | 状态 | 关键落点 |
|---|---|---|---|
| **B0** | 前端 CI 护栏 + vitest 脚手架 + node 版本统一 | ✅ | `frontend/vitest.config.ts`、`frontend/tests/setup.ts`、`frontend/tsconfig.test.json`、`.github/workflows/ci.yml`（新增 `frontend-build` job）、`.nvmrc`→22 |
| **B1** | F1 Markdown 富文本渲染 | ✅ | 新增 `frontend/src/features/chat/components/Markdown.tsx`；改 `StreamText.tsx` / `ChatPage.tsx` / `theme.css` |
| **B2** | F3 扩展解析格式 | ✅ | 新增 `parsers/spreadsheet.py`、`parsers/plaintext.py`；改 `parsers/router.py`、`main.py`、前端白名单 |
| **B3** | F2 浏览器语音输入 | ✅ | 新增 `app/api/speech.py`、`frontend/.../VoiceRecorder.tsx`；改 `config.py`、`ChatInput.tsx`、`api-client.ts`、两份代理清单 |
| **B4** | F4 单文档「快问」 | ✅ | 新增 `app/services/quickask.py`、`app/api/quickask.py`、`frontend/.../QuickAttachPanel.tsx`；改 `knowledge.py`（`index_transient`/`purge_transient`）、`pipeline.py`、`cache.py` |
| **B5** | F5 推荐追问 + 会话命名 | ✅ | 改 `services/llm.py`、`services/pipeline.py`、`services/agent/runner.py`、`api/ask.py`、`sse-client.ts`、`MessageBubble.tsx` |
| **B6** | F6 Redis 共享存储 | ✅ | 改 `core/shared_state.py`、`services/cache.py`、`services/ratelimit.py`、`services/security/quota.py`、`config.py`、`docker-compose.yml` |

---

## 二、计划之外的三处修正（均为「按计划写会出问题」）

### ① Markdown 角标识别：inline extension → **文本渲染器**

计划 §2.1.4 用 marked 的 inline extension（`start()` 找 `[#`）识别角标。
marked 的 `start(src)` 收到的是**去掉首字符**的剩余串，因此当剩余串以反引号开头
（行内代码）时，extension 看不到那个反引号，会把代码内部的 `[#1]` 误判为角标。

**改为在 `renderer.text` 里做替换**：行内代码（`codespan`）与代码块（`code`）
各有各的渲染器，根本不会走到 `text`——判定边界由 marked 的分词器保证，
不靠我们自己扫源码。安全基线（`renderer.html = () => ""`）保持不变。

### ② 表格段标记：`表格:xxx` → **`·表格:xxx`**

`knowledge.apply_chunking`（`knowledge.py:248-249`）靠 `TABLE_SOURCE_MARK = "·表格"`
把表格段与文本段分开、让表格单独成块。计划 §2.3.5 的骨架写的是 `f"表格:{sheet.title}"`，
**不含该标记** → 表格会被当普通文本切碎，表格块与 `modality=table` 全部失效。
已按 `TABLE_SOURCE_MARK` 修正。

### ③ 快问的 ACL 交互：可见清单与 `doc_id` **不能叠加**

`RetrievalFilters.allows()` 同时校验 `doc_id` 与 `visible_doc_ids`。
ACL 启用时可见清单来自知识库文档列表，而**快问文档不在知识库里** → 两者叠加会让
快问永远检索不到任何东西（表现为"上传成功、提问无证据"）。

**改为**：`doc_id` 存在时只构造 `RetrievalFilters(tenant_id, doc_id)`，
不叠加可见清单；归属由 `QuickAskService` 的 `principal.user_id` 校验负责
（临时文档本就不受知识库 ACL 管辖）。
另在 `acl_filter_of`（`dependencies.py`）里放行 `quick_` 前缀的文档，
否则应用层后过滤会把快问证据全部滤掉。

---

## 三、新增错误码

| 码 | 名称 | 场景 |
|---|---|---|
| 3004 | `QUICK_DOC_EXPIRED` | 快问临时文档已过期 / 不存在 / 归属不符（统一文案，不泄露存在性） |
| 3101 | `UNSUPPORTED_FILE_TYPE`（复用） | 快问扩展名不支持 |
| 3201 | `PARSE_FAILED`（复用） | 快问解析结果为空 |
| 3803 | `SPEECH_UNAVAILABLE` | 语音未配置 / 上游异常（3803 为新分配，**4401 已被 `RERANK_DEGRADED` 占用**） |

---

## 四、测试落地

| 层级 | 文件 | 用例数 | 覆盖要点 |
|---|---|---|---|
| 后端 unit | `tests/unit/test_spreadsheet_parser.py` | 4 | 双 sheet → 2 段 2 表；空工作簿告警；行数上限截断；损坏文件可读错误 |
| | `tests/unit/test_plaintext_parser.py` | 6 | GBK 回退；`<script>`/`<style>` 不入索引；代码逐字保留；JSON 美化；空文件报错 |
| | `tests/unit/test_quickask_service.py` | 12 | 不写 metadata / 不入 `_docs`；向量+BM25 双写；**不 bump 版本**；缓存键含 doc_id；**ACL 关闭时仍构造 doc_id 过滤**；越权/过期拒绝 |
| | `tests/unit/test_speech_sts.py` | 5 | 未配置 → 503/3803；响应与日志不泄露 access_key；上游 5xx/4xx 映射 |
| | `tests/unit/test_llm_followups.py` | 11 | 固定 3 条；LLM 失败 → []；非法 JSON → []；标题去引号 |
| | `tests/unit/test_redis_store.py` | 13 | 无客户端抛 `StoreUnavailable`（非 RuntimeError）；跨实例共享；Redis 挂掉仍可用（Fail-Open） |
| 后端 integration | `tests/integration/test_new_formats.py` | 6 | xlsx 上传→预览→检索；txt/html/代码可上传；`.xls` 明确报错；`.bin` 仍 400 |
| | `tests/integration/test_quickask.py` | 8 | 提问证据全来自该文件；同名文档不串；删除后 404/3004；非 admin 可用；他人不可读 |
| | `tests/integration/test_followups.py` | 3 | `/ask` 返回 followups；SSE 中 `followups` 在 `done` 之前；追问失败不伤主答案 |
| 前端 vitest | `tests/markdown.test.tsx` | 11 | 标题/列表/表格渲染；角标可点；原始 HTML 与事件属性被剥离；代码块/行内代码不产角标 |
| | `tests/voice-recorder.test.tsx` | 3 | 无 mediaDevices → disabled；后端不可用 → disabled；可用时不禁用 |
| | `tests/message-bubble.test.tsx` | 4 | 3 个追问按钮；点击回调；无追问/流式中不渲染 |
| | `tests/file-types.test.tsx` | 8 | 白名单含 `.xlsx`；`.xls` 读able 提示；快问面板渲染 |

**回归修正**（`.txt`/`.xlsx` 由不支持变支持导致的老断言）：

- `tests/e2e/test_api.py::test_upload_unsupported_file`：`notes.txt` → `payload.bin`（错误码仍 2003）
- `tests/integration/test_stage3.py::test_upload_unsupported_still_rejected`：`表.xlsx` → `表.bin`

**顺带修掉的存量 lint**：`app/api/{analysis,graph,ingest,knowledge}.py` 与 3 个测试文件的
未使用 import（`ruff check app tests` 此前就是红的）。

---

## 五、验证命令与结果

```bash
cd backend && uv run pytest --cov=app      # 794 passed, 6 skipped；覆盖率 84.80%（门槛 80%）
cd backend && uv run ruff check app tests  # All checks passed!
cd frontend && npm run build               # tsc -b + vite build 零错误
cd frontend && npm test                    # 4 个测试文件 / 26 个用例全绿
cd frontend && npm run typecheck:test      # 测试代码零 TS 错误
```

---

## 五之二、语音方案选型（2026-10-25 追加）

### 开源 SDK 盘点

| 方案 | 许可 | 能否在浏览器内跑 | 模型体积 | 结论 |
|---|---|---|---|---|
| Whisper（OpenAI） | MIT | 否 | tiny 75MB / base 145MB | 服务端方案最优；但 `api.openai.com` 在本机**网络不可达**（实测 12s 超时） |
| faster-whisper / whisper.cpp | MIT | 否 | 同上 | 可自建推理服务，但要多一套模型分发与 CPU 推理资源 |
| **FunASR**（阿里） | Apache-2.0 | 否 | paraformer ~220MB | 开源且成熟；百炼的 `fun-asr-flash` 就是它的 API 形态 |
| SenseVoice（FunAudioLLM） | Apache-2.0 | 否 | ~234MB | 多语种 + 情感，接口同上 |
| sherpa-onnx-web（k2-fsa） | Apache-2.0 | **是**（WASM） | 数十 MB | 浏览器内可跑，但要**把模型打进前端产物**（或让内网用户从外网下载，不可行） |
| vosk-browser | Apache-2.0 | **是**（WASM） | ~40MB | 同上，且中文精度一般 |
| transformers.js + Whisper | MIT | **是**（ONNX/WASM） | q8 约 40MB 起 | 同上，首次需从 HuggingFace 下载模型（内网不可行） |
| Web Speech API | 浏览器内置 | 是 | — | Chrome 走 Google、Safari 走 Apple：**语音出浏览器**、离线不可用，与合规要求冲突 |

**结论**：浏览器内开源方案都要求把几十~几百 MB 模型分发到客户端，在离线/内网环境不可行；
合规上"语音出浏览器"更糟。因此本次落地选择**服务端转写**。

### 最终方案：后端 ASR 代理 + 复用已配置的大模型凭据

```
浏览器                                   后端                          云厂商
  │ 录 16k/单声道/16bit 裸 PCM             │                             │
  │── POST /speech/transcribe (multipart) ─▶│ 包 44B WAV 头              │
  │                                       │── HTTPS ───────────────────▶│ 百炼 qwen3-asr-flash
  │◀──────────── {text: "..."} ───────────│◀──── 文本 + language ───────│
```

- **凭据**：复用 `DASHSCOPE_API_KEY`（`配置文件.txt` 里的千问 workspace key `sk-ws-…`），
  走百炼原生多模态接口，模型 `qwen3-asr-flash`；音频以 base64 data URI 内联，
  **不需要公网可访问 URL**（内网部署刚需）。
- **实测**：`qwen3-asr-flash` 转写一段中文 TTS 音频，耗时 721ms，返回
  "这是一个语音识别测试，query语音输入功能验证。"（`Kquery` 被识别成 `query`，属 ASR 正常误差）。
- **可换**：把 `SPEECH_ASR_PROVIDER` 改成 `openai` + `SPEECH_ASR_BASE_URL`，
  即可切到任何兼容 `/v1/audio/transcriptions` 的服务（自建 whisper.cpp / Groq / OpenAI）。
- **保留旧通路**：火山 STS 那条路（`mode=sts`）代码仍在，配了 `VOLC_APPID`/`VOLC_ACCESS_KEY`
  就自动优先使用（流式、延迟低）；两条路并存，后端用 `/speech/status` 告诉前端走哪条。

### 实测的网络可达性（决定选型的硬约束）

| 端点 | 结果 |
|---|---|
| `api.openai.com` | ✗ 12s 超时，不可达 |
| `dashscope.aliyuncs.com/compatible-mode` | ✓ 200（模型清单 180 个，含 `qwen3-asr-flash-realtime`、`fun-asr-flash`） |
| `open.bigmodel.cn`（智谱） | ✓ 200 |
| `ark.cn-beijing.volces.com`（方舟） | ✓ 200（但只有 Ark endpoint id，**没有 ASR 用的 APPID + IAM AK/SK**） |

---

## 五之三、Docker 部署与实测（2026-10-25）

### 起栈

```bash
cd project/Kquery
docker compose up -d --build
docker compose ps
```

| 服务 | 宿主端口 | 状态（实测） |
|---|---|---|
| backend | 8100 | healthy；`/healthz` 显示 `shared_state backend=redis`（**共享存储真的在用**） |
| frontend (nginx) | 8090 | healthy |
| milvus | 19531 / 9092 | healthy |
| redis | 6381 | healthy |
| etcd | — | healthy（容器内网） |

### 实测结论

| 项 | 结果 |
|---|---|
| 语音转写（容器格式 wav） | ✓ "这是一个语音识别测试，query语音输入功能验证。"（715ms） |
| 语音转写（裸 PCM = 浏览器真实载荷） | ✓ 同一文本（634ms） |
| 语音匿名访问 | ✓ 401 |
| 快问上传 → 提问 → 删除 | ✓ 证据只来自该文档；`keyword_candidates=2 / vector_candidates=2`（**BM25 + 向量双路都在跑**）；知识库文件列表为空 |
| 经 nginx 的语音链路 | ✓ `/speech/status` 与 `/speech/transcribe` 均正常代理 |
| 冒烟脚本 | ✓ **81/81 全部通过**（`scripts/smoke_api.py --base http://localhost:8100 --token <JWT>`） |

### 部署中踩到的三个坑

1. **`# syntax=docker/dockerfile:1` 让 build 直接失败**：构建器要去 Docker Hub 拉前端镜像，
   本机只能走镜像加速站 → 在"解析构建器镜像"这一步就超时。已从两个 Dockerfile 移除该行
   （用到的能力 BuildKit 内置前端都支持）。
2. **端口冲突**：`6380` 被本机 `doc_qa_redis` 占用（`6379` 也不可用）→ Kquery 的 Redis 改映射到 `6381`。
   同理 Milvus 用 `19531`、后端用 `8100`、前端用 `8090`。
3. **残留容器占用名字**：上一次部署（6 天前）留下的 `kquery-milvus` 容器名与新 compose 冲突。
   它用的是**绑定挂载**（`deploy/milvus/volumes/milvus`），新 compose 用**命名卷**
   （`kquery_milvus_data`），所以删除旧容器不丢数据，新栈是干净的库。

---

## 五之四、真实部署 + 浏览器实测发现的缺陷（2026-10-25）

只跑单测/冒烟是发现不了下面这三个的——它们都只在**多组件真跑起来**时暴露。

### 缺陷 1（严重）：Redis Lua 调用签名错 → **毒化整个共享状态**

`redis-py` 的签名是 `eval(script, numkeys, *keys_and_args)`，
而实现写成了 `eval(script, keys_list, args_list)`：第 2 个位置参数被当成
`numkeys`（收到 list）→ 每次限流都抛 `Invalid input of type: 'list'`。

**危害不止于限流**：`RedisStore` 在任何失败时把 `_healthy=False`，
于是 `SharedState._call` 之后**所有**操作都静默走进程内——缓存不再跨副本、
配额额度按副本数放大、快问元数据只落本进程（重启即丢，
下次启动清扫还会把"看起来没人认领"的临时文档全删掉）。

**修法**：
1. `eval` 按真实签名调用；
2. `healthy=False` 时按冷却时间自动重连（`ping` → 必要时重建连接），
   **降级必须能自愈**，不能被一次偶发失败永久钉死；
3. 单测的 Redis 替身改成真实签名，并加"Redis 可用时限流不得降级"的断言
   （回退到旧实现该用例会红——已验证）。

### 缺陷 2：快问元数据扫描用错属性 → 多副本清扫形同虚设

`RedisQuickAskStore.list_doc_ids()` 用 `self._state._prefix` 取键前缀，
但 `SharedState` 没有这个属性（前缀在 `RedisStore._prefix`）→ 抛
`AttributeError` 被 `except` 吞掉 → 只留一行 warning，
**Redis 侧的快问文档永远扫不到**（多副本启动清扫只清得到本进程的）。

### 缺陷 3（运维级）：启动清扫阻塞启动 → 容器 unhealthy / crash loop

快问清扫每个文档要打两次 Milvus（query + delete），实测**约 10s/个**；
而清扫跑在 lifespan 里 → 几十个残留就把启动拖成几分钟 →
容器在启动期就被判 unhealthy，`restart: unless-stopped` 会把它打进 crash loop。

**修法**：清扫移到**后台线程**（维护任务不该挡在服务前面），
并同时限**个数**与**时间预算**（`limit=20, budget_s=10`），剩下的留给下次。

### 修复后的实测

| 项 | 结果 |
|---|---|
| 后端启动 | 20s 内 healthy（修复前 3 分钟仍 unhealthy） |
| Redis 限流桶 | `kquery:ratelimit:bucket:default` 真实写入，0 条 eval 失败日志 |
| 快问元数据 | `kquery:quick:*` 真实写入 Redis，跨副本可见 |

---

## 五之五、收口 doc-qa 的剩余缺口（F9 / F10，2026-10-25）

对照 doc-qa 的 46 项用户可见功能逐条比对后，判定"值得补"的只有 3 项
（DeepDOC 本地 OCR / 版面分析 **明确不补**：一律改用云服务 API，不保留本地模型扩展点）。

### F9 · PPT/PPTX 解析

| 项 | 内容 |
|---|---|
| 后端 | 新增 `app/services/parsers/presentation.py`（`python-pptx`）：逐页抽文本框 + 表格 + 备注页；表格单独成段（source 带 `·表格` 标记 → `modality=table` 单独成块）；形状数/页数/段长三重上限 |
| 白名单 | `SUPPORTED_EXTENSIONS` 加 `.pptx`；`.ppt`（二进制 BIFF）明确报"请另存为 .pptx" |
| 前端 | `file-types.ts` 白名单 + 上传向导文案同步 |
| 依赖 | `python-pptx>=1.0.2` |
| 为什么不用移植 doc-qa 的实现 | doc-qa 的 `ppt_parser.py` 只有 68 行，依赖同样是 `python-pptx`；它单独成模块只因为 DeepDOC 那套 OCR/版面栈太重。**PPT 是结构化 XML，本来就不需要 OCR**——直接读 OOXML 更准更快 |

### F10 · 引文增强（前端）

| 能力 | 实现 |
|---|---|
| 参考汇总卡片 | 答案区下方 `N 条引文 · 来自 M 个文件`（按 doc_id 去重），点"查看引文"展开过程区并滚动定位 |
| 引文面板关键字搜索 | 证据列表上方搜索框，命中 内容/来源/文件名/chunk_id 任一即算；显示"匹配 X / Y 条"与无结果提示 |
| 导出回答为 TXT | 新增 `src/lib/export-txt.ts`：`buildAnswerText`（问题+答案+证据清单+追踪号）+ `downloadAnswerAsTxt`（Blob → 临时 `<a download>`）。**纯前端**，不新增后端接口 |

### F10 · 快问临时文档绑会话（对齐 doc-qa 的"会话内快速解析"）

| 层 | 实现 |
|---|---|
| 元数据 | 快问临时文档新增 `session_id` 字段（上传时可带） |
| 校验 | 绑定到会话的文档，**只能在同一会话里提问**：跨会话 → 404 + 3004（不泄露"文档存在"） |
| 生效面 | `POST /quickask/upload`（Form 字段 `session_id`）、`POST /quickask/ask`（body `session_id`）、`GET /quickask/documents?session_id=`（列出会话内文档）、以及普通 `/ask` 与 `/ask/stream` 带 `doc_id` 时的同一校验 |
| 前端 | `uploadQuickAsk(file, sessionId)` → `QuickAttachPanel` 透传 `sessionId` → `ChatPage` 传当前 `activeSession` |
| 未绑定文档 | 仍可在任意会话使用（"上传即问"的原有用法不受影响） |

### 测试

| 层 | 用例 |
|---|---|
| 后端 unit | `test_presentation_parser.py`（6）：逐页/表格/备注/空文件/损坏文件/路由注册 |
| 后端 integration | `test_new_formats.py` 新增 2 项（pptx 入库可检索、旧 .ppt 可读错误）；`test_quickask_session.py`（10）：绑定/跨会话拒绝/三条提问通道都拦/会话文档列表 |
| 前端 vitest | `citation-features.test.tsx`（7）：参考卡片计数、搜索过滤、无结果提示、导出文本内容与下载触发；`quickask-session.test.tsx`（3）：上传带 session_id、缺席时不带、accept 含 .pptx；`file-types.test.tsx` 新增 2 项（.pptx 白名单、.ppt 可读提示） |
| 浏览器 | `e2e_full.py` 新增 8 项断言：参考卡片计数/文件数、搜索无结果提示、导出 TXT 触发下载、PPTX 上传有反馈 |

---

## 五之六、F11 · OCR / 转写「一律云服务 API」（2026-10-26）

用户要求：**删除任何"部署本地模型做 OCR"的路径，一律走云服务 API。**
逐项核对后确认：Kquery 代码里**本来就没有**本地模型（无 torch / onnxruntime /
transformers / tesseract 依赖，也没有任何权重加载逻辑）——本地 OCR 只存在于
doc-qa（DeepDOC ONNX）与旧版计划文档的措辞里。本次做的是**把这条约束变成
代码里钉死的规则**，而不是删代码。

### 改动

| 层 | 改动 |
|---|---|
| `services/parsers/ocr.py` | 模块 docstring 写明架构约束与 4 条理由；`build_ocr_engine` 的 provider 白名单收紧为 `{dashscope, mock}`，**非云 provider 直接报错**（原来会静默退回 Mock —— 线上表现为"图片入库了但没有文字"） |
| `services/parsers/audio.py` | 同样收紧：转写 provider 白名单 `{dashscope, mock}`，本地模型 provider 报错 |
| `core/config.py` | 新增 `ocr_provider`（auto / dashscope / mock）+ `resolved_ocr_provider`；**启动期校验**拒绝 local / onnx / paddleocr 一类取值，并入 `config_items`（前端"系统配置"页可见） |
| `main.py` | OCR 引擎改用 `settings.resolved_ocr_provider`（不再跟随全局 provider），可单独强制云端 |
| `.env.example` | 新增 `OCR_PROVIDER=auto` + 说明 |
| 文档 | 计划文档 §2.7 删掉"保留 `pdf_local_vision` 扩展点作为未来选项"的说法 → 改为**绝对不保留**；`doc/analysis/模块.md`、`backend/README.md` 同步声明 |

### 守卫测试（`tests/unit/test_ocr_cloud_only.py`，25 项）

这是本次的**核心产出**：把架构约束写成会红的测试，而不是文档里的一句话。

| 守卫 | 断言 |
|---|---|
| provider 白名单 | `dashscope` / `mock` 之外的 provider（local / onnx / self-hosted / paddleocr / deepdoc / offline）一律抛错，且错误文案含"云服务 API" |
| 启动期校验 | `OCR_PROVIDER=local` 在 `validate_startup()` 里被拦下；`ocr_provider=dashscope` 但无凭据也拦下 |
| 依赖扫描 | `pyproject.toml` 的依赖声明里不得出现 onnxruntime / torch / transformers / tensorflow / paddleocr / easyocr / pytesseract / sentence-transformers / funasr / opencv 等本地模型运行时 |
| 源码扫描 | `app/**.py` 里不得出现 `import onnxruntime` / `from transformers import` / `from_pretrained(` / `pytesseract.image_to_string` / `easyocr.Reader(` / `*.onnx` / `*.safetensors` 等加载痕迹（只匹配**真正的加载动作**，不误伤"明确拒绝本地模型"的错误文案） |

**已验证守卫有效**：临时往 `ocr.py` 注入 `import onnxruntime` → 用例立刻红；撤销后恢复绿。

### 顺带修掉的存量缺陷

`tests/integration/test_stage9_security.py::test_daily_limit` 是**时间相关的脆弱用例**：
上一版把"距零点秒数"按 **UTC** 算，而实现按 **本地时区** 算（`_seconds_until_midnight`
用 `time.localtime`）→ 东八区在**本地 00:00~08:00** 这 8 小时窗口里两者差 8 小时，
测试必红。已改成与实现同口径（本地零点），任何时刻、任何时区都成立。
（这次是在 00:19 跑全量回归时才暴露出来。）

### 真机验证（Docker 栈，云端 qwen-vl-ocr）

| 步骤 | 结果 |
|---|---|
| 上传 PNG（PIL 生成的中英文文字图） | 200，`status=indexed`，1 块 |
| 预览块内容 | `Kquery Cloud OCR Verification 2026 / Total 128 ques…` ← **云端识别结果** |
| 资产 | `kind=image` ✓ |
| 检索 | 问"每月有多少个问题额度？" → 答案"每月有128个问题额度 [#1]"，首条证据即 OCR 文本 |

即：**上传图片 → 云端 OCR → 入库 → 检索 → 回答**整链在部署形态下真实跑通。

---

## 六、遗留与后续

| 项 | 说明 |
|---|---|
| 语音通路 | **已闭环**：默认走后端 ASR 代理（百炼 `qwen3-asr-flash`），Docker 栈上实测转写成功；火山 STS 那条路代码保留，配了 `VOLC_APPID`/`VOLC_ACCESS_KEY` 即自动优先使用（未配） |
| 浏览器内语音识别 | 未采用：开源方案（sherpa-onnx-web / vosk-browser / transformers.js）都要求把几十~几百 MB 模型分发到客户端，内网不可行；盘点表见 §五之二 |
| 本地 OCR / 转写模型 | **不提供任何本地或自部署模型路径**：OCR 一律云服务 API（`qwen-vl-ocr`）、转写一律云服务 API（`paraformer-v2`）。约束已由 `backend/tests/unit/test_ocr_cloud_only.py` 守卫（25 项：provider 白名单、依赖扫描、源码扫描） |
| Redis 真实环境冒烟 | **已完成**：Docker 栈上 `/healthz` 显示 `shared_state backend=redis`、`degraded_operations=0` |
| `.xls` 支持 | 明确不支持（openpyxl 读不了 BIFF），入口给可读错误；如确有需求需单独立项 |
| 近似缓存的跨副本语义 | Redis 后端下**精确命中跨副本、近似命中仅本副本**（Redis 上做全表余弦扫描不可行），已在 `cache.py` 的类文档中写明 |
| 阶段 8 报告中心 | 仍按既定决策**不移植**（§2.7/§2.8 论证不变） |
