"""数据模型（阶段 1 README §4）。

字段按"将来要进数据库"的标准设计：标识稳定可组合、租户字段从第一天就存在、
证据是一等实体（带标识、可追溯、可评分）。
"""
from pydantic import BaseModel, Field, computed_field

# 文档状态机：阶段 1 仅 uploaded / parsing / indexed / failed，阶段 3 扩展为七态
DOC_STATUS_UPLOADED = "uploaded"
DOC_STATUS_PARSING = "parsing"
DOC_STATUS_INDEXED = "indexed"
DOC_STATUS_FAILED = "failed"


class DocumentRecord(BaseModel):
    """文档记录：描述"库里有哪些文档"。"""

    doc_id: str  # 全局唯一，阶段 1 由文件名推导
    file_id: str  # 上传动作生成的标识，界面上可复制
    filename: str  # 原始文件名，展示用，与 doc_id 分离
    tenant_id: str  # 租户标识，阶段 1 取默认值
    source_path: str  # 原始文件路径
    chunk_count: int = 0  # 入库后回填，用于校验完整性
    short_chunk_count: int = 0  # 超短块数（质量红线：占比 >10% 需调整切块参数）
    chunker: str = ""  # 本次实际使用的切块器（策略可能被自动选择覆盖）
    version: int = 1  # 索引版本号：重新解析时 +1，旧版本块保留供历史引用溯源
    content_hash: str  # 判断"文件变了但名字没变"
    status: str = DOC_STATUS_UPLOADED
    error: str | None = None  # 失败原因（hover 查看）
    size: int = 0
    uploaded_at: str = ""
    # 阶段 3 扩展：章节大纲（解析时产出并持久化，预览页左侧结构）
    outline: list[dict] = Field(default_factory=list)
    # 阶段 9 扩展：文档级 ACL（默认拒绝——未显式授权不可见）
    acl_allow: list[str] = Field(default_factory=list)  # 身份表达式：user:/role:/group:
    acl_deny: list[str] = Field(default_factory=list)  # 拒绝名单（优先于授权）
    acl_public: bool = True  # 租户内公开（阶段 9 前的存量文档视为公开，新文档可收紧）


class Chunk(BaseModel):
    """块记录：文档被切分后的最小检索单元。"""

    chunk_id: str  # {doc_id}#{seq}，重建索引保持不变，绝不用随机值
    doc_id: str
    seq: int  # 从 1 起递增，与 chunk_id 中序号一致
    content: str  # 非空，空块入库前被过滤
    tenant_id: str  # 冗余存储，检索时一次过滤完成
    source: str  # 页码或章节，用于溯源展示
    is_short: bool = False  # 超短块标记（保留但标记）
    # ---- 阶段 3 扩展（§4.3：四处扩展必须同时存在）----
    chunker: str = "recursive"  # 切块器标识：recursive / semantic / parent_child / raptor
    level: int = 0  # 层级：0 为叶子，越大越抽象（RAPTOR 摘要树）
    parent_id: str = ""  # 父块标识，叶子为空；命中子块后回溯父块补全上下文
    modality: str = "text"  # 模态：text / table / image / audio


# 文档附加状态（阶段 3）：已下线（删除索引但保留源文件与记录）
DOC_STATUS_OFFLINE = "offline"


# ---- 阶段 3：文档状态机（7 态，任一处理态可跳转至失败）----
FILE_PENDING = "pending"  # 待上传
FILE_UPLOADING = "uploading"  # 上传中
FILE_QUEUED = "queued"  # 排队中
FILE_PARSING = "parsing"  # 解析中
FILE_CHUNKING = "chunking"  # 切块中
FILE_INDEXING = "indexing"  # 索引中（不可取消：块与向量一致性的临界区）
FILE_DONE = "done"  # 已完成
FILE_FAILED = "failed"  # 失败（error_stage 记录失败阶段）

# 任务整体状态
TASK_QUEUED = "queued"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_PARTIAL_FAILED = "partial_failed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"

# 可取消的阶段（索引中及之后取消会留下半成品索引，必须禁止）
CANCELLABLE_STAGES = {FILE_QUEUED, FILE_PARSING, FILE_CHUNKING}


class FileRecord(BaseModel):
    """文件记录：单个文件在入库流水线中的状态（阶段 3 §4.4）。"""

    file_id: str
    task_id: str
    filename: str
    size: int = 0
    status: str = FILE_QUEUED
    stage: str = FILE_QUEUED  # 当前所处阶段（7 态之一）
    stage_progress: float = 0.0  # 阶段内进度 0~1（OCR / 转写必需）
    stage_elapsed_ms: int = 0  # 当前阶段已耗时
    error_stage: str = ""  # 失败所在阶段，决定重试粒度
    error_reason: str = ""  # 失败原因（可读）
    doc_id: str = ""  # 成功后产出的文档标识
    stats: dict = Field(default_factory=dict)  # 产物统计：页数/表格数/块数/超短块数
    #: 图谱 / 图像索引联动结果（stage-7 §5.1 入库联动）。
    #: 单独一栏而不是塞进 stats：前端要按它区分"文档已入库但图谱未构建"与"图谱构建失败"。
    graph_stats: dict = Field(default_factory=dict)


class IngestTask(BaseModel):
    """入库任务：一批文件处理到哪一步了（可查询、可重试、可取消、可断点续跑）。"""

    task_id: str
    status: str = TASK_QUEUED
    parse_policy: dict = Field(default_factory=dict)  # 解析策略（OCR / 转写开关等）
    chunk_policy: dict = Field(default_factory=dict)  # 切块策略与参数
    total_files: int = 0
    done_files: int = 0
    created_by: str = "system"  # 提交人，审计用
    created_at: str = ""
    files: list[FileRecord] = Field(default_factory=list)


class TableRecord(BaseModel):
    """表格记录：保留行列结构与页码，正文以 Markdown 进向量库（§4.3）。"""

    table_id: str
    doc_id: str
    page: int
    table_index: int
    rows: int
    cols: int
    markdown: str


class MediaAsset(BaseModel):
    """图像/音频资源：本体进对象存储，元数据与描述文本入库（§4.5）。"""

    asset_id: str
    doc_id: str
    uri: str  # 存储位置
    page: int = 0
    caption: str = ""  # 描述文本（让文字问题也能命中图片）
    hash: str = ""  # 内容摘要，跨文档去重
    tenant_id: str = "default"
    kind: str = "image"  # image / audio


class Evidence(BaseModel):
    """证据：这次回答依据了什么。编号必须与答案中的引用标记严格对应。"""

    rank: int  # 从 1 起连续，不允许跳号
    chunk_id: str  # 溯源到具体块
    score: float | None = None  # 阶段 2 起为重排相关度分，与阶段 1 相似度分不同口径
    content: str  # 正文副本（刻意冗余，避免流式返回时二次查库）
    source: str  # 来源展示，如 "产品手册.pdf · 第2页"
    doc_id: str
    filename: str
    # 阶段 2 扩展：keyword（关键词路）/ vector（语义路）/ both（两路融合命中）
    source_route: str = "vector"
    # 阶段 3 扩展：模态（text / table / image / audio）与切块器标识
    modality: str = "text"
    chunker: str = "recursive"
    # 阶段 4 扩展：该证据由哪条改写/哪个子问题带回（{kind, text}，双向定位支点）
    origin: dict | None = None
    # 阶段 5 扩展：是否被采纳进生成上下文（未采纳也必须返回，附原因）
    used: bool = True
    rejected_reason: str = ""
    # 阶段 7 扩展：图像证据的资源地址（前端渲染缩略图）；图谱证据的来源块列表
    uri: str = ""
    source_chunks: list[str] = []  # 图谱关系的支撑块（溯源）


# ---------- 阶段 7：GraphRAG 与多模态 ----------

# 实体五类（抽取语义边界，§2.1）
ENTITY_TYPES = ("Person", "Org", "Product", "Concept", "Event")
# 领域关系（带方向，§5.3：方向丢失会让答案出现事实性错误）
RELATION_TYPES = ("投资", "任职", "收购", "关联", "位于", "影响")


class EntityNode(BaseModel):
    """实体节点：名称归一化后才稳定（不归一化多跳必然断链）。"""

    entity_id: str  # 归一化后的稳定标识
    name: str  # 规范名
    type: str = "Concept"  # Person / Org / Product / Concept / Event
    description: str = ""
    aliases: list[str] = []  # 别名集合（归一化映射数据，§5.4）
    degree: int = 0  # 连接数（节点大小与重要性映射）
    community_id: str = ""
    tenant_id: str = "default"


class RelationEdge(BaseModel):
    """关系边：有方向、带强度、可溯源到块。"""

    source: str  # 起点实体规范名
    target: str  # 终点实体规范名
    relation: str  # 关系名（有方向）
    description: str = ""  # 关系描述文本（拼装证据用）
    weight: float = 1.0  # 关系强度（遍历剪枝）
    source_chunks: list[str] = []  # 支撑块（溯源，必须有）
    tenant_id: str = "default"


class Community(BaseModel):
    """社区：实体聚类分组 + 摘要（摘要入向量空间，主题问题可命中）。

    `level` 支撑多级社区（stage-7 §5.3「社区必须有层级」）：
    0 为基础层（直接由连通分量得到），1+ 为上层社区（对下层社区再聚类）。
    前端据此渲染层级缩进，检索侧据此做"先粗后细"的二级定位。
    """

    community_id: str
    members: list[str] = []  # 实体规范名（各层统一用实体名，便于直接展开）
    size: int = 0
    summary: str = ""
    generated_at: float = 0.0  # 摘要生成时刻（判断是否过时）
    model_version: str = ""  # 生成所用模型版本
    tenant_id: str = "default"
    level: int = 0  # 层级：0 基础层，越大越抽象
    parent_id: str = ""  # 上层社区的父节点（level>0 时非空）


class ImageIndexEntry(BaseModel):
    """图像索引元数据（§4.2：元数据是必需品而非可选）。"""

    asset_id: str
    caption: str  # 结构化描述：主体/属性/场景/图中文字（命中可解释性的依据）
    uri: str  # 存储位置（前端渲染）
    page: int = 0  # 来源页码
    doc_id: str = ""
    tenant_id: str = "default"
    model_version: str = ""  # 描述生成模型与版本（换模型需全量重编码的判据）
    hash: str = ""  # 内容摘要（同图多处出现去重）


class RetrievalStats(BaseModel):
    """检索统计（阶段 2 §4.4）：四个计数 + 四段耗时，随响应返回不持久化。

    排查"召回率下降"靠这八个值定位问题出在哪一段。

    对外形态按前端规范是**嵌套对象**（`latency_ms:{...}` / `candidates:{...}`），
    平铺字段保留为内部计算口径：两者同时出现在 `model_dump()` 里，
    新前端读嵌套、旧消费者读平铺，不必同步改完再上线。
    """

    keyword_candidates: int = 0
    vector_candidates: int = 0
    fused_count: int = 0
    reranked_count: int = 0
    into_context_count: int = 0
    keyword_ms: int = 0
    vector_ms: int = 0
    fusion_ms: int = 0
    rerank_ms: int = 0
    # TASK-147：本次降级（失败被跳过）的检索路。为空表示全部正常——
    # 有了它，"这一次召回为什么偏少"才有可解释的线索。
    degraded_routes: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def latency_ms(self) -> dict:
        """分阶段耗时（毫秒）。缺失段返回 0 而不是 null——前端直接做加减。"""
        return {
            "keyword": self.keyword_ms,
            "vector": self.vector_ms,
            "fusion": self.fusion_ms,
            "rerank": self.rerank_ms,
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def candidates(self) -> dict:
        """候选漏斗：召回 → 融合 → 重排 → 入上下文。"""
        return {
            "keyword": self.keyword_candidates,
            "vector": self.vector_candidates,
            "fused": self.fused_count,
            "reranked": self.reranked_count,
            "into_context": self.into_context_count,
        }


class RetrievalConfig(BaseModel):
    """运行时可调检索参数（阶段 2 前端规范 §4）：管理员经 /admin/retrieval-config 修改。

    变更不立即生效——先存为 pending，到达生效时刻后晋升为 active。

    六个参数与前端规范的"6 个可调参数"对齐：`top_k` / 权重 / 开关 / 重排 / 缓存 TTL /
    限流。其中 RRF 常数与两级召回数此前只注入到 `HybridRetriever.__init__`，
    模型里根本没有这三个字段——文档说可调，实际改不了。
    """

    vector_enabled: bool = True
    bm25_enabled: bool = True
    vector_weight: float = 1.0  # RRF 中语义路的权重
    bm25_weight: float = 1.0  # RRF 中关键词路的权重
    rerank_enabled: bool = True
    rerank_model: str = "gte-rerank-v2"
    top_k: int = 4  # 最终进入生成上下文的条数
    cache_ttl_s: int = 3600
    ratelimit_per_min: float = 60.0
    ratelimit_capacity: int = 60
    # ---- 补齐：文档声明可调但此前写死在检索器构造参数里的三项 ----
    rrf_k: int = 60  # RRF 平滑常数（论文默认 60；调小更看重头部排名）
    per_route_top_n: int = 20  # 单路召回条数
    fused_top_n: int = 20  # 融合后进入重排的候选数

    def with_fallbacks(self, settings) -> "RetrievalConfig":
        """零值字段回落��全局配置。

        存量配置 JSON 里没有新增的三个字段，用 0 表达"未设置"会静默把召回数清零，
        表现为"检索突然什么都召不回"——这类故障极难定位。
        """
        patch: dict = {}
        if self.rrf_k <= 0:
            patch["rrf_k"] = settings.rrf_k
        if self.per_route_top_n <= 0:
            patch["per_route_top_n"] = settings.retrieval_per_route_top_n
        if self.fused_top_n <= 0:
            patch["fused_top_n"] = settings.retrieval_fused_top_n
        if self.cache_ttl_s <= 0:
            patch["cache_ttl_s"] = settings.cache_ttl_s
        if self.ratelimit_per_min <= 0:
            patch["ratelimit_per_min"] = settings.ratelimit_refill_per_min
        if self.ratelimit_capacity <= 0:
            patch["ratelimit_capacity"] = settings.ratelimit_capacity
        return self.model_copy(update=patch) if patch else self


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    top_k: int | None = Field(default=None, ge=1, le=10)
    # 阶段 6：Agentic 模式与会话续聊
    session_id: str | None = None  # 携带则在会话上下文中提问（证据跨轮累积）
    mode: str = "auto"  # auto（默认线性）/ agent（状态机）
    user_id: str | None = None  # 归属判定（也可走 X-User-Id 头）
    # F4 快问：限定只检索该临时文档（不传 = 走全知识库）
    doc_id: str | None = None


class AskResult(BaseModel):
    """answer + evidence 双字段契约：答案给人看，证据给前端做引用跳转。"""

    answer: str
    evidence: list[Evidence]
    trace_id: str
    elapsed_ms: int
    # 阶段 2 扩展
    from_cache: bool = False  # 命中语义缓存
    reranked: bool = True  # False 表示重排降级（本次未精排），前端轻提示
    retrieval: RetrievalStats | None = None  # 检索过程统计（供检索过程可视化）
    # 阶段 4 扩展：Query 预处理分析记录（意图/改写/子问题/路由/降级标注）
    analysis: dict | None = None
    # 阶段 5 扩展：答案判定（拒答是一等公民，走成功状态码）
    verdict: str = "answered"  # answered / refused
    refusal_reason: str = ""  # no_content / insufficient_evidence / permission_denied
    answer_source: str = ""  # evidence（有据）/ parametric（通用知识兜底）/ ""（拒答或旧数据）
    confidence: float | None = None  # 0~1；None = 未校验（评分器/自检器降级）
    regenerated: bool = False
    unsupported_claims_count: int = 0
    crag: dict | None = None  # {branch, rounds: [{round, branch, avg_score, evidence_count, refined_query}]}
    # 阶段 5 扩展：自检明细（哪些声明被剔除、用的什么方式校验）
    self_check: "SelfCheckRecord | None" = None
    # 阶段 9 扩展：因权限被过滤的条数（只报数量，绝不暴露被过滤文档的元信息）
    acl_filtered: int = 0
    # F5 扩展：推荐追问（LLM 不可用时为空列表，绝不影响主答案）
    followups: list[str] = []


class SelfCheckRecord(BaseModel):
    """幻觉自检记录（stage-5 §8.2 SELF_CHECK 实体）。

    `checked_claims` / `removed_claims` 让"重生成过"可量化——
    剔除过声明的样本是最有价值的改进素材，只存一个 int 就浪费了。
    """

    method: str = "llm_claim_review"  # 校验方式（换校验器时便于分组统计）
    checked_claims: int = 0  # 送检的事实性声明条数
    removed_claims: int = 0  # 因无据被剔除的条数
    passed: bool = True  # 是否通过（false = 重生成后仍有无据声明）
    unsupported_claims: list[str] = []  # 被判无据的声明原文（前端提示"剔除了哪句"）
    reasoning: str = ""
    model_version: str = ""
    elapsed_ms: int = 0


class CragRound(BaseModel):
    """CRAG 单轮记录（stage-5 §8.2 CRAG_ROUND 实体）。

    落库而非只随响应返回：否则事后无法复盘"这次为什么被拒答"。
    """

    trace_id: str = ""
    question: str = ""
    tenant_id: str = "default"
    round: int = 1
    branch: str = "correct"  # correct / ambiguous / incorrect
    avg_score: float = 0.0
    evidence_count: int = 0
    refined_query: str = ""
    created_at: float = 0.0


AskResult.model_rebuild()
