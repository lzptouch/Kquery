# 阶段 3 附录：RAG 文档解析技术调研（PDF / DOC / 表格 / 版面）

> 本文是 [`README.md`](./README.md) 第 3 章「多模态解析层」的技术选型附录。
> 资料来源为 2024-2026 年公开技术文章与官方文档（见文末），检索日期 2026-10-02。
>
> **与全系列模型基线的关系**：调研覆盖的所有方案被划分为两类——
> ① **解析库**（纯规则/几何算法，无权重、无 GPU，本项目可直接使用）；
> ② **模型能力**（需加载本地权重的方案，与「模型统一走千问平台」基线冲突，本项目**不采用其本地推理形态**，对应能力由千问平台接口承担）。

---

## 一、PDF 解析技术全景

### 1.1 主流方案横向对比

| 方案 | 类型 | 输出 | 标题层级 | 表格还原 | 双栏阅读顺序 | 扫描件 | 速度(20页) | 本项目适配性 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **PyMuPDF** (fitz) | 解析库 | 纯文本 + 几何信息 | 需按字号自建 | `find_tables` 几何识别 | `sort=True` 可部分修复 | ❌ | ⚡ 0.8s | ✅ **采用**（文字流 + 表格流） |
| pypdf / PyPDF2 | 解析库 | 纯文本 | ❌ | ❌ | ❌ | ❌ | 快 | 已被 PyMuPDF 替代 |
| pdfplumber | 解析库 | 文本 + 坐标 | ❌ | 需手写规则推断 | ❌ | ❌ | 1.2s | 备选（精细坐标场景） |
| Camelot / Tabula | 解析库 | 表格 CSV | — | 有框线表格高精度 | — | ❌ | 慢（依赖 Ghostscript/Java） | 表格专项备选 |
| unstructured | 解析库 + 可选模型 | 半结构化元素 | 有限 | 较差 | 基本正确 | 可配置 | ~5s | 不采用（中文与表格弱于专项组合） |
| **Docling** (IBM) | **本地模型**（DocLayNet + TableFormer + EasyOCR） | Markdown + JSON | ✅ 完整 | ✅ 跨页/合并单元格 | ✅ | ✅ 内置 | ~8s | ❌ 本地模型，**违反模型基线**；其「布局理解」能力由 `qwen-vl-ocr` 承担 |
| **MinerU** (OpenDataLab) | **本地模型**（版面分析 + 公式识别） | Markdown + JSON | ✅ | ✅ 含无线框表格 | ✅ | ✅ | CPU 慢 / GPU 快 | ❌ 同上；公式 LaTeX 化是其独有能力 |
| RAGFlow DeepDoc | 本地模型 | 结构化 | ✅ | ✅（中文合同/论文强） | ✅ | ✅ | 中 | ❌ 同上 |
| LlamaParse / GROBID | 云服务 / 本地服务 | 结构化 | ✅ | ✅ | ✅ | ✅ | — | 外部依赖，不采用 |

> 评分参考（CSDN 实测，20 页 IEEE 双栏论文）：PyMuPDF 3/10（纯文字质量）、pdfplumber 4/10、unstructured 6/10、Docling 9/10。**分差主要来自版面理解（标题/双栏/表格），而非文字抽取本身。**

### 1.2 三条技术路线的本质差异

1. **文字抽取路线**（PyMuPDF / pypdf / pdfplumber）：把 PDF 当字符流，速度快、零依赖，但**不理解版面**——标题层级丢失、双栏混排、表格变噪声。
2. **版面理解路线**（Docling / MinerU / DeepDoc）：先做布局检测（标题/正文/表格/图/公式分区），再做结构识别与阅读顺序重建。质量上限最高，**但全部是本地深度学习模型**（DocLayNet、TableFormer、YOLO 系），与「不部署本地模型」的基线冲突。
3. **云端 VLM 路线**（本项目采用）：版面理解交由千问 `qwen-vl-ocr` / `qwen-vl-max`——对倾斜、多栏、反白、表格线混杂场景天然稳健，且**免部署、与全系列模型同平台同口径**。文字型 PDF 仍走解析库（成本为零），**只有规则解析不达标时才升级 VLM**——这就是阶段 3「PDF 双轨 + OCR 补充」架构的依据。

### 1.3 PDF 表格抽取专项

| 技术 | 原理 | 适用 | 局限 |
| --- | --- | --- | --- |
| PyMuPDF `find_tables` | 几何线框检测 + 单元格归属 | **有框线表格**，零依赖 | 无线框表格漏检 |
| Camelot (lattice/stream) | 线框检测 / 空格对齐推断 | 有框线高精度 | 依赖 Ghostscript；无框线(stream)精度不稳 |
| Tabula | Java 几何规则 | 有框线 | 需 JVM |
| TableFormer（Docling） | Transformer 结构识别 | 跨页、合并单元格 | 本地模型，本项目不用 |
| **千问 `qwen-vl-ocr`** | 视觉语言模型版面理解 | 扫描件表格、无线框、倾斜 | 按页计费、秒级时延 → **走异步任务** |

**工程结论**（已在代码中落地）：
- 文字型 PDF 的表格：`find_tables` 几何识别 → Markdown，**单独成块**（`modality=table`），行列数与页码存 `TableRecord` 供前端还原与校验（防错行红线：行列数与原表不一致即标记）；
- 表格未被几何识别命中、或扫描件表格：整页走 `qwen-vl-ocr`，提示词要求表格区域用 Markdown 表格表示；
- 表格**绝不进通用切块器**（拦腰切断后列含义丢失，表格问答命中率崩塌——多篇实测均指出这一点）。

### 1.4 阅读顺序与双栏问题

- 双栏/多栏论文按字符流抽取会左右栏混杂。解析库侧的缓解：PyMuPDF `page.get_text(sort=True)` 按几何位置排序（**已落地**）；更彻底的做法是按 block 聚类分列（版面理解，归 VLM）。
- **经验法则**：规则解析产物若出现"相邻句子语义断裂"（可在语义切块时观测到距离矩阵异常），整页升级 OCR，**不在规则侧无限补丁**。

### 1.5 PDF 标题大纲提取（预览页左侧结构）

解析库可行做法：遍历 `page.get_text("dict")` 的 span，字号显著大于正文中位数（或含粗体 flag）的连续行判定为标题，按字号分级。**已在代码中落地**（替代此前"PDF 无大纲"的缺口），与 Markdown 的 `#` 大纲在预览页统一渲染。

---

## 二、DOC / DOCX 解析技术

### 2.1 格式差异决定路线

| 格式 | 本质 | 解析方式 |
| --- | --- | --- |
| `.docx` | ZIP 包（document.xml + styles.xml + numbering.xml） | **python-docx**（段落/样式/表格对象模型完备） |
| `.doc`（97-2003） | OLE2 复合二进制 | python-docx 不支持；**LibreOffice headless 转 .docx**（`soffice --convert-to docx`）后按 .docx 解析；antiword/catdoc 只能抽纯文本丢结构 |

### 2.2 python-docx 结构化提取要点（已落地）

1. **标题层级**：段落 `style.name` 为 `Heading 1~9`（中文模板可能为 `标题 1~9`）→ 直接产出大纲与章节分段，**按标题结构分段而非固定长度**（调研中多篇实测：结构化切片 top-5 命中率比固定长度提升 20%~30%，表格多的文档提升最显著）；
2. **表格**：`document.tables` 行列对象模型 → 渲染 Markdown 单独成块（与 PDF 表格同一契约），行列数存 `TableRecord`；
3. **顺序还原**：段落与表格在 XML 中的真实顺序需迭代 `document.element.body` 子元素（`w:p` / `w:tbl`）而非分别遍历——否则"先全部段落、后全部表格"会打乱文档叙事顺序；
4. **列表编号**：`numbering.xml` 决定列表序号，文本不含编号字符——本阶段不还原编号（标记为遗留项，影响列表密集文档的断句）；
5. **图片**：`word/media/` 中的嵌入图——抽存为 `MediaAsset`，描述文本由 `qwen-vl-max` 生成（阶段 7 的图像描述能力前置复用，本阶段先存档）。

### 2.3 老式 .doc 的工程处理（已落地）

- 检测 LibreOffice（`soffice`）→ `--headless --convert-to docx` 转换后走 .docx 管线；
- 无 LibreOffice 时**诚实失败**（明确提示"请安装 LibreOffice 或另存为 .docx"），不静默降级为纯文本抽取（结构全丢的"成功"比失败更有害）。

---

## 三、结构化解剖与切块的最佳实践（调研共识）

1. **按结构切，不按固定长度切**：遇 H1/H2 标题打包前段为一块；表格独立成块；超长正文再按段落边界拆。结构化切片对 top-5 命中率提升 20%~30%（CSDN 实测），且"绝不更差"。
2. **双份文本策略**："**索引用的文本要干净，展示用的文本要完整**"——表格竖线/图片占位符污染 embedding，向量化文本应清理；给用户与 LLM 看的文本保留完整 Markdown。两者用同一 `chunk_id` 关联（本项目证据正文字段即"完整文本"，embedding 输入即该字段，表格场景下已验证可用；更精细的双文本分离列入阶段 7 候选）。
3. **大块文档防 OOM**：超长 PDF 分批解析、断点续跑（与本项目 `IngestTask + FileRecord.error_stage` 的重试粒度一致）。
4. **解析器单例常驻**：解析器/引擎在应用启动时构建一次（本项目在 lifespan 装配），不在请求路径上重复初始化。
5. **按文档类型裁剪流水线**：文字型 PDF 关 OCR；扫描件走 OCR；纯文字书刊只留基础抽取（本项目 `parse_policy` 的开关设计依据）。
6. **回归评测驱动**：每次改解析/切块策略跑固定评测集（Recall@K），"改好了还是改坏了，数据说话"——与阶段 2 的调参纪律一致。

---

## 四、本项目解析层最终架构（调研后的落地形态）

```
.md / .markdown ─→ MarkdownParser（全文 + # 大纲）
.docx ──────────→ DocxParser（Heading 样式大纲 + 结构分段 + 表格 Markdown + 嵌入图存档）
.doc ───────────→ LibreOffice 转 .docx → DocxParser；无 LibreOffice 则诚实失败
.pdf ───────────→ PdfParser（PyMuPDF）
                   ├─ 文字流：sort=True 阅读顺序 + 按页分段 + 字号法大纲
                   ├─ 表格流：find_tables → Markdown 单独成块 + TableRecord
                   └─ 红线：单页文字量异常 → warning 提示走 OCR
.png/.jpg ──────→ OcrParser（千问 qwen-vl-ocr + 二次清洗 + 版面信息留存）
.mp3/.wav ──────→ AudioParser（千问 paraformer-v2，时间戳分段）
```

**解析库 vs 模型能力分界**（全系列红线，再次确认）：PyMuPDF / python-docx / jieba / numpy 均为无权重规则库；OCR、转写、摘要、生成全部千问平台接口。**本调研中的 Docling / MinerU / DeepDoc 等本地模型方案明确不引入**，其版面理解收益由 `qwen-vl-ocr` 以云端形态等价获得。

---

## 五、参考来源

**PDF 解析对比**：
- 《docling实战：PDF结构化解析与RAG数据预处理最佳实践》（CSDN，2026-09，含四工具 20 页 IEEE 实测对比表与结构化切片 20~30% 提升数据）：https://blog.csdn.net/weixin_32495691/article/details/166553495
- 《RAG 吃 PDF 老丢版面，用 Docling 把表格和标题结构都解析出来》（百家号，2026-08）：https://baijiahao.baidu.com/s?id=1873208497372415534
- 《RAG系统最优「文档开源解析方案」》（知乎，2025-08）：https://zhuanlan.zhihu.com/p/1937130286149268763
- 《破解PDF解析难题：RAG中高效解析复杂PDF的最佳选择》（腾讯云，2025-03）：https://cloud.tencent.com/developer/article/2505901
- 《10个RAG开发必备PDF解析库》（汇智网，2024-11）：https://www.hubwiz.com/blog/top10-pdf-parsing-libs-for-rag/

**表格抽取**：
- 《Docling文档解析实战：PDF/扫描件结构化、表格识别与RAG集成指南》（CSDN，2026-09，含 pypdf/pdfplumber/Tabula/Docling 表格对比）：https://blog.csdn.net/weixin_42523543/article/details/166560129
- TableFormer 论文出处（CVPR 2022）与 Docling 模型库：https://modelscope.cn/models/ds4sd/docling-models
- 《PDF文档解析API对比：MinerU vs Docparser vs PDF.co》（含有框线/无线框表格对比，2026-04）：https://blog.csdn.net/lingxie2512/article/details/160480214

**MinerU / 版面理解**：
- 《文档解析换 MinerU，扫描版 PDF 和公式也能转成干净 Markdown》（百家号，2026-08）：https://baijiahao.baidu.com/s?id=1873209185616897586
- 《MinerU + RAG 集成实战：从 PDF 结构化解析到精准检索》（腾讯云，2026-04）：https://cloud.tencent.com/developer/article/2662542

**DOC/DOCX**：
- 《为RAG优化Word与PDF文档解析并还原版面表格》（阿里云开发者，2024-08，LibreOffice 转 docx 方案）：https://developer.aliyun.com/article/1587651
- 《Python提取Word表格数据教程（含.doc/.docx）》（腾讯云，2025-07，LibreOffice 环境检测与转换代码）：https://cloud.tencent.com/developer/article/2545926
- 《高效实现Word文件解析与多格式转换实战》（CSDN，2025-10，python-docx 对象分层模型）：https://blog.csdn.net/weixin_35750747/article/details/153217069

**预处理流水线**：
- 《RAG效果翻倍的秘密：高质量文档预处理流水线全解析》（CSDN，2025-10）：https://blog.csdn.net/sara_han/article/details/153935590
- 《RAG系统：文档解析与文本分块的常见方案》（夜雨聆风，2026-04，含 DeepDoc 介绍）：https://www.yeyulingfeng.com/494945.html
