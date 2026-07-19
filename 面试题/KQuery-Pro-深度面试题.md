# KQuery Pro — 深度技术面试题（200道）

> **文档位置**：`doc/KQuery-Pro-深度面试题.md`  
> **面向项目**：KQuery Pro 企业级 GraphRAG 知识检索与问答系统  
> **题目总量**：约 200 道，统一为较高难度  
> **题目结构**：每道题包含「原理阐述」「场景应用」「问题排查」三个考察维度，并附详细参考答案。  
> **命题依据**：基于 `backend/app/services/`、`backend/app/services/graph/`、`backend/app/services/graphrag/`、`backend/app/api/`（含 llm/notes/mindmap/web_search/speech/memory/tasks/auth/knowledge_base/skills 等路由）、`backend/app/core/`、`backend/app/repositories/`、`frontend/src/` 及 `doc/` 下架构/设计文档的实际实现细节。

---

## 目录

1. [RAG 模块：架构原理、检索策略、优化及重排机制](#rag-模块)
2. [知识图谱模块：构建流程、存储方案与查询逻辑](#知识图谱模块)
3. [项目综合技术栈：开发全流程关键技术决策与难点排查](#项目综合技术栈)

---

## RAG 模块

### 1.1 架构原理与流水线

#### 1. `RAGService` 与 `RAGEngine` 的边界差异是什么？

**考察维度**：原理阐述（RAG 门面与检索编排分层）；场景应用（何时直接调用 `RAGService`，何时使用 `RAGEngine`）；问题排查（检索结果为空时优先检查哪层日志）。

**参考答案**：`RAGService`（`app/services/rag_service.py`）是 RAG 对外门面，负责文档处理、批量处理、集合管理、服务状态查询；`RAGEngine`（`app/services/retriever.py`）是检索编排层，只负责搜索、插入、清空、统计。`RAGService.process_document_with_chunks` 选择加载器、分块、调用 `OpenAIEmbeddingService.embed_parallel` 并行向量化，再写入 Milvus；`RAGService.query_documents` 委托 `RAGEngine.search`。排查时先看 `RAGService` 的输入校验与加载器日志，再进入 `RAGEngine.search` 的向量检索、HyDE、重排序链路。

---

#### 2. 为何 `RAGService.process_document_with_chunks` 对 PDF/Word 显式构造加载器，而不是统一走 `LoaderRegistry`？

**考察维度**：原理阐述（注册表模式与特殊加载器参数）；场景应用（新增文件格式如何接入）；问题排查（上传 `.docx` 后向量化结果为空）。

**参考答案**：`process_document_with_chunks` 根据 `file_extension` 显式构造 `PDFLoader`、`WordLoader`、`MarkdownLoader`，其余才走 `LoaderRegistry.get_loader`。PDF/Word 需要开启 `extract_images`、`extract_tables`、`understand_images` 等参数，显式构造确保这些多模态选项正确传递。新增格式只需继承 `BaseLoader` 并在 `LoaderRegistry` 注册。排查：确认 `LoaderRegistry.is_supported` 返回 True；检查 `load_document_chunks` 是否返回空；确认 `chunk.get_full_content()` 是否包含内容，以及 `embed_parallel` 是否过滤空字符串。

---

#### 3. `RAGEngine.search` 中 HyDE、向量检索、重排序、时间衰减的调用顺序及设计考量？

**考察维度**：原理阐述（检索流程各阶段位置）；场景应用（根据查询类型决定是否启用 HyDE）；问题排查（召回结果明显过时）。

**参考答案**：执行顺序：1) 若启用 HyDE，调用 `HyDEGenerator.generate` 生成假设文档并替换查询；2) 使用 `query_document_chunks` 向量检索，召回 `top_k * 3`；3) 若启用重排序，调用 `RerankerService.rerank` 精排；4) 若启用时间衰减，调用 `TimeDecayScorer.score` 基于 `created_at` 调整分数。顺序设计：先扩展语义，再扩大召回，再精排，最后压制陈旧内容。结果过时时检查 `enable_time_decay` 与 `time_decay_half_life_days`，以及 chunk 元数据中 `created_at` 是否正确。

---

#### 4. `RAGEngine.hybrid_search` 与 `search` 的核心区别？为何 `hybrid_search` 仍保留向量检索？

**考察维度**：原理阐述（混合检索互补性）；场景应用（何时优先启用 `hybrid_search`）；问题排查（BM25 无贡献）。

**参考答案**：`search` 是纯向量检索；`hybrid_search` 引入 BM25 词频检索，用 `RRFusionEngine.fuse` 做 RRF 融合。向量擅长语义泛化，BM25 擅长精确关键词匹配。`hybrid_search` 保留向量检索因为 RRF 需要两路结果共同投票。若 BM25 无贡献，检查 `_bm25_search` 是否因 `candidates` 为空未执行，或查询无有效字符导致 `query_terms` 为空。

---

#### 5. `RetrievalResult` 为何设计为 `frozen=True, slots=True`？

**考察维度**：原理阐述（不可变对象与 `__slots__`）；场景应用（高并发检索结果传递）；问题排查（修改 `score` 时报错）。

**参考答案**：`frozen=True` 使实例不可变，避免并行处理或缓存传递时被意外修改；`slots=True` 禁用 `__dict__`，减少内存占用。检索结果在多个组件间传递更安全。修复：使用 `dataclasses.replace(result, score=new_score)` 创建新实例。

---

#### 6. RAG 引擎延迟初始化子服务的三个好处？

**考察维度**：原理阐述（Lazy Initialization）；场景应用（单元测试替换子服务）；问题排查（启动成功但首次查询失败）。

**参考答案**：1) 减少启动开销；2) 支持按需降级；3) 便于依赖注入与测试。首次查询报错说明延迟初始化失败，应查看 HyDE（`agent_api_base`）、Reranker（/health）、RRF、时间衰减配置的日志。

---

#### 7. `DocumentChunk.get_full_content()` 为何合并图像/表格描述后再向量化？

**考察维度**：原理阐述（多模态文档向量化策略）；场景应用（产品参数截图被文字查询命中）；问题排查（图像内容检索不到）。

**参考答案**：将 `content` + `image_description` + `table_description` 拼接，使纯文本查询也能命中多模态内容。排查：查看 `chunk_metadata` 是否包含 `image_description`，以及 `understand_images` 是否开启。

---

#### 8. `asyncio.Semaphore(max_concurrency=3)` 在批量处理中的作用及参数过大风险？

**考察维度**：原理阐述（异步信号量并发控制）；场景应用（100 个 PDF 批量上传）；问题排查（Milvus 连接超时）。

**参考答案**：限制同时处理文档数，默认 3。设置过大可能导致 Milvus 或 Embedding API 并发超限、内存耗尽、文件句柄不足。出现超时应降低 `max_concurrency`，检查 `connection_pool.py` 连接池大小和 `settings.perf.max_concurrency`。

---

#### 9. `add_text` 与 `process_document` 在向量化路径上的差异？

**考察维度**：原理阐述（两种文本入库接口设计意图）；场景应用（API 推送实时文本）；问题排查（`add_text` 写入后检索不到）。

**参考答案**：`process_document` 需要文件路径，经过加载器解析、分块、向量化；`add_text` 直接接受文本，调用 `RAGEngine.insert_document_chunk` 单条插入，省略文件加载和批量分块。检索不到时确认返回 True，检查 Milvus 写入和查询集合名一致。

---

#### 10. `fetch_k = top_k * 3` 的设计考量和改进方向？

**考察维度**：原理阐述（召回候选与精度权衡）；场景应用（高精度问答场景调整 `fetch_k`）；问题排查（Top-5 结果质量差但向量分数高）。

**参考答案**：确保重排序和多样性惩罚有足够候选。取值过大增加延迟，过小可能漏掉高质量结果。改进：根据查询复杂度动态调整。若 Top-5 质量差，可能 HyDE 假设文档偏离原查询，或未结合 BM25/图谱。

---

### 1.2 检索策略：向量、BM25、HyDE、RRF

#### 11. `OpenAIEmbeddingService.embed` 对空输入和 API 未配置的容错处理？

**考察维度**：原理阐述（嵌入服务降级与 Mock 数据）；场景应用（本地开发无 Embedding API）；问题排查（生产环境所有查询返回相同向量）。

**参考答案**：先过滤空字符串；若 `is_llm_configured("embedding")` 为 False，使用 Mock 向量。生产环境所有向量相同，说明判定未配置，检查 `embedding_api_key`、`embedding_api_base` 是否被错误缓存。

---

#### 12. `embed_parallel` 如何保持输出顺序与输入一致？

**考察维度**：原理阐述（异步批量处理顺序保持）；场景应用（批量向量化 1000 个 chunk）；问题排查（第 N 个 chunk 向量与内容不匹配）。

**参考答案**：保留原始索引 `[(i, t) ...]`，分批并发后按原始顺序补齐。若出现错位，检查某批次返回向量数量是否与输入一致。

---

#### 13. `chunk_text` 为何使用栈而非递归？分隔符优先级如何影响切分？

**考察维度**：原理阐述（迭代分割 vs 递归）；场景应用（中文技术文档参数设置）；问题排查（长段落被切成单字符块）。

**参考答案**：使用栈避免递归深度限制。分隔符优先级：`\n\n > \n > 句号 > 标点 > 空格 > ""`。优先语义边界保证 chunk 完整。单字符块说明 `chunk_size` 过小或文本无可用分隔符。

---

#### 14. `overlap` 机制如何实现？为何是拼接而非简单复制？

**考察维度**：原理阐述（重叠保持语义）；场景应用（跨 chunk 边界句子命中）；问题排查（相邻 chunk 重复内容过多）。

**参考答案**：`prev_chunk_tail` 保存上一块尾部，拼接到当前块头部。保证跨边界语义不丢失。重复过多时减小 `overlap`（默认 50）或增大 `chunk_size`。

---

#### 15. HyDE 假设文档限制 100 字符的影响？

**考察维度**：原理阐述（HyDE 语义关系）；场景应用（复杂多条件查询）；问题排查（启用 HyDE 后效果变差）。

**参考答案**：让假设文档更接近真实片段长度，减少 LLM 成本。简单查询效果好；复杂查询可能信息压缩。效果变差可能原因：温度过高、模型质量不足、原查询过短引入噪声。可禁用 HyDE 或调整 `max_tokens`。

---

#### 16. `HyDEGenerator.generate` 的故障降级策略？

**考察维度**：原理阐述（HyDE 降级）；场景应用（离线 demo 环境）；问题排查（日志频繁出现失败提示）。

**参考答案**：若 LLM 未配置使用 Mock 数据；若调用失败退化到原始查询。短查询影响小。优化：确认 `agent_api_key/base` 正确、检查 LLM 服务可用性。

---

#### 17. `TextTokenizer` 在 jieba 不可用时的降级及影响？

**考察维度**：原理阐述（中文分词 vs 空格分词）；场景应用（未安装 jieba 时 BM25 可用性）；问题排查（中文 BM25 分数全为 0）。

**参考答案**：降级为 `text.split()` 按空格分词，中文无空格导致句子被当作一个 token。排查日志中 jieba 警告，安装后重建索引。

---

#### 18. BM25 索引为何需要重建底层模型？增量更新性能影响？

**考察维度**：原理阐述（rank_bm25 不支持真正增量更新）；场景应用（频繁新增文档维护策略）；问题排查（新增文档后检索不到）。

**参考答案**：`rank_bm25` 必须重新计算 IDF 统计量。频繁新增时重建成本线性增长。改进：批量新增减少重建频率。检索不到检查 `add_documents` 是否调用、索引是否正确序列化。

---

#### 19. `sanitize_customer_id` 和 `validate_customer_id` 的安全职责？

**考察维度**：原理阐述（路径穿越防护）；场景应用（多租户索引隔离）；问题排查（索引文件存在但加载失败）。

**参考答案**：`validate_customer_id` 检查输入合法性；`sanitize_customer_id` 清理 `..`、 `/` 等危险字符，防止路径穿越。索引路径为 `safe_customer_id.pkl`，无法跳出缓存目录。若文件存在但加载失败，检查 sanitize 后文件名是否一致。

---

#### 20. RRF 公式 `weight * rrf_k / (rrf_k + rank)` 为何不直接使用原始分数？

**考察维度**：原理阐述（RRF 数学原理）；场景应用（向量与 BM25 量纲不一致）；问题排查（融合后 Top-1 与向量检索不一致）。

**参考答案**：RRF 使用排名倒数避免量纲不一致。`rrf_k=60` 降低高排名过度优势。融合后 Top-1 变化正常。

---

### 1.3 优化与重排机制

#### 21. `RerankerService.rerank` 调用失败时如何降级？

**考察维度**：原理阐述（Cross-Encoder 重排序 API）；场景应用（重排序服务不可用）；问题排查（重排序结果为空但候选非空）。

**参考答案**：API 失败时调用 `_fallback_rerank`，基于原始分数乘以衰减因子排序。排查：检查 `rerank_api_base` 是否可达、模型名正确、候选 `content` 字段非空。

---

#### 22. `_fallback_rerank` 为何使用 `score * (1.0 - i * 0.01)`？

**考察维度**：原理阐述（基于原始排名的简单重排序）；场景应用（降级结果可用性）；问题排查（降级后结果质量下降）。

**参考答案**：按原始排名给微小衰减保持原顺序。可靠性低。临时优化：增大 `fetch_k`、调整 RRF 权重让更可靠的路占主导。

---

#### 23. `TimeDecayScorer` 的半衰期公式及业务调整？

**考察维度**：原理阐述（指数时间衰减数学模型）；场景应用（新闻 7 天 vs 产品手册 365 天）；问题排查（重要历史文档被排后）。

**参考答案**：`decay_factor = 0.5 ** (age_days / half_life_days)`。若重要文档被排后，检查 `time_decay_half_life_days` 是否过短、文档 `created_at` 元数据是否正确。

---

#### 24. `RerankerService` 的 `_initialized` 标记为何在健康检查失败时也设为 True？

**考察维度**：原理阐述（乐观初始化策略）；场景应用（内网环境无外网重排序服务时仍发起请求）；问题排查（重排序服务宕机后首次请求超时）。

**参考答案**：避免每次查询都进行健康检查，降低延迟。但首次真实请求会超时并降级。改进：定期后台健康检查 + 优雅标记不可用。

---

### 1.4 多模态与文档处理

#### 25. `PDFLoader` 和 `WordLoader` 如何提取文档中的图片和表格？

**考察维度**：原理阐述（多模态文档解析策略）；场景应用（产品规格 PDF 上传后的检索准确性）；问题排查（图片被提取但描述为空）。

**参考答案**：`load_document_chunks` 中提取嵌入图片保存到临时文件（`image_path`），调用 `ImageUnderstandingService`（qwen-vl-plus）生成描述；提取表格结构化数据，调用 `TableUnderstandingService` 生成表格描述。描述为空时检查阿里云 VL API Key 配置、图片分辨率是否低于 100×100 阈值。

---

#### 26. `ImageUnderstandingService` 的图像描述级别（brief/standard/detailed）如何影响检索质量？

**考察维度**：原理阐述（图像理解详细程度与语义丰富度）；场景应用（产品图片 vs 流程图的不同级别选择）；问题排查（图像检索准确率低）。

**参考答案**：brief 级别只输出简短标签，standard 输出完整描述，detailed 输出详尽说明。详细级别语义更丰富但成本更高。准确率低时可尝试升级描述级别或检查图片分辨率。

---

#### 27. 表格理解服务如何从 HTML 和文本中提取表格结构？

**考察维度**：原理阐述（BeautifulSoup HTML 解析 vs 模式匹配）；场景应用（网页表格 vs Markdown 表格）；问题排查（表格数据提取乱码或列错位）。

**参考答案**：HTML 表格用 BeautifulSoup 解析 `<table>`、`<tr>`、`<td>` 标签；文本表格用竖线/制表符分隔模式匹配。提取后调用 LLM 生成自然语言摘要。列错位时检查原始表格是否使用了 colspan/rowspan 合并单元格，文本表格是否对齐。

---

#### 28. 文件加载器插件体系如何支持新增格式？

**考察维度**：原理阐述（Registry 模式与 BaseLoader 抽象基类）；场景应用（`.tex` 或 `.rmd` 新格式接入）；问题排查（上传已知格式文件报 "unsupported" 错误）。

**参考答案**：`BaseLoader` 定义 `load` 和 `load_document_chunks` 接口；新增加载器继承 BaseLoader 实现解析逻辑；通过 `LoaderRegistry.register` 注册扩展名到加载器类的映射。`get_loader` 根据文件扩展名自动选择。报 unsupported 时检查扩展名是否已注册、文件是否带点号、扩展名是否大写忽略。

---

#### 29. `TextChunk` 数据模型中 `char_offset` 的作用及计算方式？

**考察维度**：原理阐述（字符偏移量的溯源作用）；场景应用（定位查询结果的原始位置）；问题排查（offset 为 -1 的含义）。

**参考答案**：`char_offset` 记录 chunk 在原文档中的起始字符位置，用于溯源和定位。通过 `text.find(content, search_start)` 计算。若为 -1 表示无法找到准确位置，可能是原文本已被预处理（如去除空白）导致片段不完全匹配。

---

#### 30. `load_and_chunk_files` 中 `ThreadPoolExecutor` 而非 `asyncio` 的原因？

**考察维度**：原理阐述（CPU 密集文件解析 vs IO 密集网络请求）；场景应用（批量大量文件处理的选择）；问题排查（文件并行解析速度不达标）。

**参考答案**：PDF/Word/Excel 解析（PyMuPDF/python-docx/openpyxl）主要是 CPU 密集型操作（文本提取、页面渲染），`ThreadPoolExecutor` 更合适。IO 密集操作（如网络 API 调用）才用 `asyncio`。速度不达标时增大 `max_workers_io` 或换用 `ProcessPoolExecutor`。

---

### 1.5 性能、可观测性与异常处理

#### 31. `RAGEngine` 如何解决向量检索与 BM25 检索分数量纲不一致的问题？

**考察维度**：原理阐述（RRF 消除量纲差异）；场景应用（余弦距离 [-1,1] vs BM25 [0,∞) 的融合）；问题排查（融合后某路检索完全主导）。

**参考答案**：RRF 使用排名倒数替代原始分数，不依赖具体量纲。若某路完全主导，检查该路召回排名是否过于集中（如所有候选排名都是 Top-1），可增大 `rrf_k` 降低高排名优势，或降低该路权重。

---

#### 32. 如何诊断 RAG 检索慢的问题？

**考察维度**：原理阐述（检索各阶段耗时分解）；场景应用（用户反馈搜索响应慢）；问题排查（Grafana 面板中哪项指标最关键）。

**参考答案**：查看 `HybridSearchResult.performance_metrics` 各阶段耗时：`question_classification`（规则匹配快）、`entity_extraction`（LLM 调用慢）、`graph_expansion`（Neo4j 查询慢）、`vector_search`（Milvus 搜索慢）、`result_fusion`（计算快）。Grafana 面板观察 `vector_search` 延迟 >100ms 时优化 Milvus 索引（`ef`、`M` 参数）；`graph_expansion` 慢时减少邻居深度或限制数量。

---

#### 33. RAG 检索中 `top_k` 取值对 LLM 答案生成有何影响？

**考察维度**：原理阐述（检索块数量与 LLM 上下文窗口的关系）；场景应用（产品规格查询 vs 开放性问题）；问题排查（答案包含太多冗余信息）。

**参考答案**：`top_k` 越大 LLM 上下文越丰富但噪声也越多，可能导致答案不聚焦。产品规格查询 `top_k=3` 即可；开放性问题 `top_k=5-8`。冗余过多时降低 `top_k` 或启用多样性惩罚。

---

#### 34. `RAGEngine.search` 中为何在重排序后才做时间衰减？

**考察维度**：原理阐述（重排序模型对时间信息的关注度）；场景应用（重排序优先相关性还是时效性）；问题排查（新文档因时效性低被排后）。

**参考答案**：重排序使用 Cross-Encoder 模型评估查询-文档语义相关性，对时间无关。时间衰减在后可保留相关性排序前提下压制陈旧内容。若新文档被排后，检查半衰期参数或文档是否缺少 `created_at` 元数据。

---

#### 35. 多租户场景下 RAG 检索如何隔离不同知识库的数据？

**考察维度**：原理阐述（Milvus 集合级别隔离）；场景应用（租户 A 查询不应命中租户 B 文档）；问题排查（跨租户数据泄露风险）。

**参考答案**：每个知识库使用独立的 Milvus collection（`collection_name` 参数），Cypher 查询通过 `WHERE e.kb_id = $kb_id` 过滤。`kb_isolation.py` 通过 `get_collection_name` 添加租户前缀。排查：检查 API 层是否从 JWT 正确提取 `tenant_id` 并透传到 Milvus/Neo4j 查询。

---

## 知识图谱模块

### 2.1 构建流程：NER、RE、Agent 模式

#### 36. `GraphBuilderService` 的两种抽取模式（Pipeline vs Agent）如何选择？各有什么优劣？

**考察维度**：原理阐述（分步 NER+RE 与统一 Agent 的架构差异）；场景应用（准确率敏感 vs 成本敏感场景）；问题排查（Agent 模式返回空实体但 Pipeline 正常）。

**参考答案**：Pipeline 模式先 `EntityExtractionService.extract_entities` 再 `RelationExtractionService.extract_relations`，两步独立执行，便于调试和中间结果检查。Agent 模式通过 `LLMGraphAgent.extract` 单次调用完成 NER+RE，减少 API 次数，LLM 理解能力更强。Agent 模式失败时降级检查：`llm_graph_agent` 初始化是否成功、API Key 是否配置、文本长度是否超过 `max_text_length`（4000 字符）。

---

#### 37. `EntityExtractionService.extract_entities` 中规则引擎与 LLM 如何并行执行并合并结果？

**考察维度**：原理阐述（双引擎并行 + 合并消歧流程）；场景应用（规则高精度匹配 + LLM 高召回覆盖）；问题排查（规则结果与 LLM 结果冲突时谁优先）。

**参考答案**：使用 `asyncio.gather` 同时启动规则引擎（14 条正则）和 LLM NER。合并时：名称+类型去重，取置信度较高的；规则优先保留（来源标记为 "rule"），LLM 补充规则遗漏的实体。冲突时规则优先，因为规则置信度（0.8-1.0）通常高于 LLM（0.6-0.95）。最终经 `EntityDisambiguator` 消歧后输出。

---

#### 38. `PatternMatcher` 的 14 条预置正则如何保证高精度？匹配到非实体名称时如何处理？

**考察维度**：原理阐述（正则 NER 的模式设计）；场景应用（从简历中抽取人名和组织）；问题排查（规则匹配了过多无效实体）。

**参考答案**：每条模式经过严格命名（如 `employed_by_1`、`works_for`），针对中文常见句式（"X 是 Y 的 CEO"、"X 在 Y 工作"），`confidence` 预设在 0.84-0.95。匹配时验证捕获组的实体名称长度（≤100 字符），若提供实体列表则检查源名称与实体名称的包含关系。无效过多时缩小正则范围或降低置信度阈值。

---

#### 39. `LLMRelationExtractor.extract` 中如何限制输入文本和实体数量以避免 Token 超限？

**考察维度**：原理阐述（LLM 上下文窗口管理策略）；场景应用（长文档仅有部分包含关系信息）；问题排查（关系抽取结果为空且无错误）。

**参考答案**：`max_text_chars=3000` 截断文本，`max_entities=30` 限制实体列表。若结果为空且无错误，检查文本截断后是否丢失了关系信息，或实体数量超出限制导致关键实体未被送入 LLM。

---

#### 40. `RelationValidator.normalize_entity_names` 中的 `SequenceMatcher` 模糊匹配阈值（0.85）是否适用于所有场景？

**考察维度**：原理阐述（Levenshtein 相似度在实体匹配中的应用）；场景应用（长实体名 "阿里巴巴集团" vs "阿里巴巴"）；问题排查（短实体名如 "A公司" 与 "B公司" 误匹配）。

**参考答案**：`SequenceMatcher` 计算字符级相似度，阈值 0.85 对中文名称（2-6 字）较合适。短名称（如 2-3 字）容易误匹配。改进：使用词级 Jaccard 相似度、引入实体类型约束、或对短名称降低阈值。

---

#### 41. `GraphBuilderService._find_entity_id` 中为何使用名称匹配而非 ID 直接映射？

**考察维度**：原理阐述（LLM 输出实体名与规范名不一致的模糊匹配）；场景应用（LLM 抽取 "阿里" 但实体列表中是 "阿里巴巴集团"）；问题排查（关系引用失败但实体已创建）。

**参考答案**：LLM/规则抽取的关系中的 source/target 是名称字符串，与实体的规范名称可能不一致。`_find_entity_id` 通过模糊匹配找到最佳匹配的实体 ID。匹配失败时记录 `result.warnings` 并跳过该关系。排查时查看 `result.warnings` 中 "关系缺少来源实体" 的提示。

---

#### 42. `_attach_document_metadata` 生成的 Document 元节点和 `MENTIONED_IN` 关系对图谱溯源有何作用？

**考察维度**：原理阐述（数据血缘追踪的图谱表示）；场景应用（从查询结果追溯到源文档）；问题排查（实体无来源文档信息）。

**参考答案**：创建 `DOCUMENT` 类型的元节点存储文档元数据、实体通过 `MENTIONED_IN` 关系指向该节点，实现从实体反向查找源文档。若实体无来源，检查 `entities_to_store` 在调用 `_attach_document_metadata` 前是否为空，或 `doc_id` 是否为 None。

---

#### 43. `LLMGraphAgent` 中 `GraphExtractionParser._parse_response` 如何解析 LLM 的非标准 JSON 输出？

**考察维度**：原理阐述（LLM 输出格式的错误容忍与修复）；场景应用（LLM 返回了 Markdown 包围的 JSON 块）；问题排查（解析后实体列表为空）。

**参考答案**：先尝试提取 ````json ... ```` 代码块，再尝试找最外层 `{}`。若 JSON 解析失败，执行三级修复：移除尾随逗号、修复单引号、修复无引号的键。空结果时检查 `raw_response` 原始输出，可能是 LLM 返回了非 JSON 格式。

---

<!-- CONTINUE -->

