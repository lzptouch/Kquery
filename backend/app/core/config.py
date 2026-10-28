"""全局配置：阈值、模型名、路径一律外置（设计原则 5：配置外置）。"""
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/ 根目录

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ---- 千问平台（阿里云百炼 / DashScope）----
    dashscope_api_key: str = ""
    # auto: 有 key 走 dashscope，无 key 走 mock（便于无凭据环境下的功能测试）
    model_provider: str = "auto"  # auto | dashscope | mock
    # OCR provider：**一律云服务 API**，只允许 auto | dashscope | mock。
    # auto = 跟随 model_provider；dashscope = 强制云端；mock = 测试。
    # 没有 local/onnx/paddleocr 这类本地模型选项（见 parsers/ocr.py 模块说明）：
    # 本地 OCR 要在镜像构建期烤权重或运行时从 HuggingFace 下载，
    # 内网环境两者都不可行，最终会变成"部署后 OCR 静默不可用"。
    ocr_provider: str = "auto"
    llm_model: str = "qwen-plus"
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024
    temperature: float = 0.0  # RAG 场景固定 0，事实性优先

    # ---- RAG 参数----
    chunk_size: int = 300
    chunk_overlap: int = 50
    top_k: int = 4
    min_chunk_chars: int = 20  # 超短块标记阈值

    # ---- 混合检索（ README §2.1 / §7）----
    retrieval_per_route_top_n: int = 20  # 单路召回条数（最终 Top-K 的 3~5 倍）
    retrieval_fused_top_n: int = 20  # 融合后进入重排的候选数
    rrf_k: int = 60  # RRF 平滑常数（论文默认值）
    bm25_k1: float = 1.5  # 词频饱和
    bm25_b: float = 0.75  # 长度归一
    rerank_model: str = "gte-rerank-v2"

    # ---- 缓存 / 限流 ----
    cache_ttl_s: int = 3600  # 语义缓存 TTL（1 小时）
    # 语义近似的相似度下限：问题向量余弦 ≥ 该值即视为"同一个问题"，
    # 从而让"保修政策 vs 保修条款"这类改写也能命中（ 语义级缓存）。
    cache_sim_threshold: float = 0.95
    cache_max_scan: int = 2000  # 近似检索每请求最多比对的候选条目数
    ratelimit_capacity: int = 60  # 令牌桶容量（突发峰值）
    ratelimit_refill_per_min: float = 60.0  # 令牌补充速率（个/分钟，按租户）
    retrieval_config_apply_delay_s: int = 300  # 检索参数变更延迟生效（秒）

    # ----  增强：扫描版 PDF 自动 OCR ----
    pdf_ocr_enabled: bool = True  # 扫描页自动转 OCR（按页混合策略）
    pdf_ocr_dpi: int = 120  # 页面渲染分辨率（VL 模型识别质量与成本的平衡点）
    pdf_ocr_max_pages: int = 200  # 单文档 OCR 页数上限（成本护栏）
    pdf_scan_min_chars: int = 30  # 单页文本层字符数低于此值判定为扫描页
    # 索引版本化：重新解析生成新版本而非覆盖，历史块保留供引用溯源
    index_version_keep: int = 3  # 保留的历史索引版本数（不含当前版；超出后按版本号从旧到新下线）

    # ---- 音频转写 ----
    transcribe_model: str = "paraformer-v2"  # 文档强制基线（千问平台，不引本地模型）
    audio_max_chunk_s: float = 600.0  # 单次转写提交的最长时长（超出切分后并行提交）
    audio_min_sample_rate: int = 16000  # 低于此采样率标"转写质量可能不佳"
    audio_split_on_silence: bool = True  # 切分点优先落在静音段，避免切断词
    audio_max_workers: int = 4  # 分片并行上限：再高只会加剧平台限流
    # 业务代码版本
    code_version: str = ""  # 留空则评测侧读 KQUERY_CODE_VERSION → git 短 SHA → unknown

    # ---- Query 预处理 ----
    preprocess_enabled: bool = True
    intent_model: str = "qwen-turbo"  # 意图识别：高频轻量判定压到最低档（成本控制）
    query_expanders: str = "fusion,stepback"  # 启用的改写器（hyde 与 fusion 互斥，不同时启用）
    fusion_n: int = 3  # RAG-Fusion 改写数量（经验最优点）
    query_budget: int = 6  # 单次提问的检索查询预算（超出截断并标记未使用）

    # ---- CRAG 校验与幻觉自检 ----
    crag_enabled: bool = True
    # 阈值必须按自有评测集的分数分布标定（ 的红线）。
    # 本系统标定口径（2026-10-02 混合语料实测）：
    #   干净 Markdown 的相关检索 Top-2 均分 ≈ 0.45~0.78；
    #   OCR 教材扫描件的相关检索 Top-2 均分 ≈ 0.18~0.25（版面噪声压缩分数）；
    #   无关问题 ≈ 0.05~0.08。
    # → 上阈值 0.14（OCR/对比摊薄场景可过）、下阈值 0.08（噪声可拒）。
    # 换评分器 / 换语料构成必须重新标定（不同评分器分数不可比较）。
    crag_correct_th: float = 0.14
    crag_incorrect_th: float = 0.08
    crag_score_top_n: int = 2  # 评分聚合：Top-N 重排分均值（Top-4 全量均值会被长尾稀释）
    crag_max_iterations: int = 2  # 精炼-重检索循环上限（必需，防无限循环）
    selfcheck_enabled: bool = True  # 幻觉自检（qwen-plus 判定）

    # ---- 通用知识兜底（TASK-新）：知识库未覆盖时由模型自身知识回答 ----
    # 原则：诚实但有用——承认知识库没有，同时不浪费用户一次提问。
    # 红线：敏感词/HITL stop/预算耗尽的拒答不兜底（见 doc/analysis/通用知识兜底回答.md §4.2）。
    parametric_fallback_enabled: bool = True  # 总开关；False 恢复纯拒答行为
    # 判据口径与 reflect 的 ANSWER_THRESHOLD（reflector.py）一致：evidence_score < 阈值
    # 或零证据时走兜底。不要用 crag_correct_th（重排分口径，与综合分不可比较）。
    parametric_fallback_score_threshold: float = 0.6
    parametric_fallback_confidence: float = 0.3  # 固定置信度（低于有据回答，不参与自检）

    # ---- Agentic RAG ----
    agent_enabled: bool = True  # /ask 带 session_id 或 mode=agent 时走状态机
    agent_max_steps: int = 6  # 单次规划最大步骤数
    agent_planner_model: str = "qwen-plus"  # 规划/反思档位（过低会规划出无效步骤）
    agent_session_ttl_s: int = 30 * 86400  # 会话快照 30 天自动过期
    agent_tool_timeout_s: int = 30  # 单步工具超时（第三层熔断）
    agent_no_progress_limit: int = 3  # 无效进展熔断窗口
    # ---- checkpointer（断点续跑 / 多轮状态继承 / 回放 / HITL 前提）----
    # sqlite：单文件、事务安全、跨进程可读（推荐生产）
    # memory：进程内、不落盘（测试与单机轻量部署）
    # none  ：显式关闭，等价于不启用续跑与 HITL
    agent_checkpointer: str = "sqlite"
    agent_checkpoint_path: str = ""  # 空则用 {data_dir}/agent_checkpoints.db
    # ---- 三预算治理（0 = 不限）----
    # 预算耗尽**不报错**，降级为「基于已有证据作答 + incomplete 标记」。
    agent_token_budget: int = 20000      # 单次 Agent 运行的 token 上限
    agent_cost_limit_yuan: float = 0.5   # 单次运行成本上限（元）
    agent_deadline_ms: int = 60000       # 单次运行墙钟上限（毫秒）
    # 每千 token 单价（元），按模型名配置；缺省则成本记为 0 且前端标「未知」。
    # 显式配置而非内置：内置价格会过期，过期价格比没有价格更危险。
    agent_price_per_1k_json: str = '{"qwen-plus": 0.0008, "qwen-max": 0.02, "qwen-turbo": 0.0003}'
    # ---- 人在环（HITL）----
    # 默认关闭：HITL 对用户是**打断**，需要前端配合渲染"待补充"卡片才能用，
    # 在后端单方面打开会让所有低置信问题都变成空答案。
    # 启用前提：agent_checkpointer != none（否则无法挂起）。
    agent_hitl_enabled: bool = False
    agent_hitl_score_threshold: float = 0.5  # 低于此充分度触发人工介入
    # ---- 上下文组装治理 ----
    # 0 = 不限制（按条数控制）。默认给明确预算：条数相同不代表长度相同，
    # 一段 3000 字的表格块能直接撑爆上下文。
    context_token_budget: int = 6000
    # MMR 相关性权重：1.0 = 纯按相关分（关闭多样性）；0.7 = 兼顾多样性
    context_mmr_lambda: float = 0.7
    # ---- 结构化日志 ----
    log_level: str = "INFO"
    log_format: str = "text"  # text（本地开发友好）/ json（生产，日志平台可索引）
    log_debug_sample_rate: float = 0.1  # 仅对 DEBUG 生效；INFO 及以上不采样
    # ---- 答案校验节点 ----
    # 默认关闭：它多花一次 LLM 调用，且会把"先答后发"变成"可能答两次"。
    # 与 reflect 互补——reflect 评证据够不够，verify 评答案站不站得住。
    # ---- ReAct 单步决策循环 ----
    # 默认仍走 plan_execute：ReAct 每轮多一次 LLM 调用、行为变化大，
    # 在检索评测集落地前贸然切换等于无法验证的改动。
    agent_mode: str = "plan_execute"  # plan_execute | react
    # ---- 长期记忆 ----
    memory_enabled: bool = True
    memory_max_per_tenant: int = 500  # 上限：记忆不能无限增长（注入成本会失控）
    # ---- 共享状态后端 ----
    # 空 = 进程内（单副本）。多副本必须配 Redis，否则限流/配额实际额度 × 副本数
    redis_url: str = ""
    redis_key_prefix: str = "kquery:"
    # 语义缓存后端：auto（有 Redis 用 Redis）/ memory / redis
    # redis 模式下 redis_url 为空 → 启动即失败（配置错误不该拖到运行期才暴露）
    cache_backend: str = "auto"
    cache_max_entries: int = 10000
    agent_verify_enabled: bool = False
    agent_max_verify_rounds: int = 1  # 最多回退重做几轮（防"改了还是不通过"死循环）

    # ---- GraphRAG 与多模态 ----
    graph_enabled: bool = True
    # 图存储后端（sqlite = 增量持久化且原子；memory = 旧的内存 + JSON 全量落盘）
    graph_store_backend: str = "sqlite"
    # ---- OpenTelemetry ----
    # 默认关闭：未部署 collector 时开启只会白占内存与 CPU。
    # 启用但未配 endpoint 会明确告警并降级为 NoOp（不阻断启动）。
    otel_enabled: bool = False
    otel_exporter_endpoint: str = ""  # 如 http://localhost:4318/v1/traces
    otel_service_name: str = "kquery"
    otel_sample_ratio: float = 1.0  # 无上游 trace 时的本地采样比例
    graph_path: str = ""  # 空则用 {data_dir}/graph/graph.json（sqlite 时自动换 .db 后缀）
    entity_sim_threshold: float = 0.85  # 实体消歧向量相似度阈值（经验值，需按数据调优）
    graph_bfs_depth: int = 2  # 遍历深度（≥3 噪声剧增）
    graph_seed_top_k: int = 3  # 种子实体上限（防子图爆炸）
    community_max: int = 20  # 社区摘要数量上限（成本控制）
    # 社区层级数：1 = 单层连通分量（默认，成本最低）；
    # >1 逐级重叠合并出粗粒度主题，摘要成本随层级线性增长（）
    # 默认 2 层（原为 1 = 层级关闭）。1 层只有连通分量级别的社区，
    # GraphRAG 论文里的"层级摘要"（社区套社区）完全没启用；2 层能覆盖
    # "全局问题用粗粒度摘要、局部问题用细粒度"这一核心用法。
    community_levels: int = 2
    # 层级合并阈值：成员重叠比例 ≥ 该值才合并（调低更粗、调高更细）
    community_merge_threshold: float = 0.3
    image_caption_model: str = "qwen-vl-max"  # 图像描述模型（换版本需全量重编码）

    # ---- 企业护栏 ----
    # 默认开启认证（拒绝静默裸奔）。无凭证来源时启动校验失败——
    # 要么给静态凭证表（auth_tokens_json），要么启用 JWT（auth_jwt_enabled + secret），
    # 要么显式 auth_enabled=false 承认开发/测试模式。
    auth_enabled: bool = True
    auth_tokens_json: str = ""  # {"token": {user_id, tenant_id, roles, groups}}
    auth_users_json: str = ""  # 用户目录 JSON；留空则读写 data/users.json（持久化）
    # 标准令牌档（企业生产）：HS256 JWT，带签发方/受众校验
    auth_jwt_enabled: bool = False
    auth_jwt_secret: str = ""  # HS256 签名密钥（生产应由 Secret 注入）
    auth_jwt_issuer: str = ""
    auth_jwt_audience: str = ""
    # ---- 账号密码登录（第四档：登录端点自签发上面这套 JWT）----
    # 默认关闭：不改变任何现有部署的认证路径。开启后 auth_jwt_secret 成为必需
    # （签发与验签共用同一把密钥），且必须保证至少有一个账号有口令，否则服务起来了
    # 却谁也进不去——那种故障比启动失败难查得多，故在 validate_startup 里硬拦。
    auth_password_enabled: bool = False
    auth_registration_enabled: bool = False  # 自助注册总开关；新账号一律最小权限
    auth_session_ttl_s: int = 900  # 登录令牌有效期（默认 15 分钟）
    auth_bootstrap_admin_password: str = ""  # 首次启动为 admin 写入口令；仅当其尚无口令时生效
    acl_enabled: bool = True  # 文档级 ACL（检索前租户过滤 + 检索后 ACL 双保险）
    # 知识库授权开关：合并进 acl_filter_of 同一闭包；为假时 KB 判定整体跳过，
    # 行为逐位退回现状。生产必须与 auth_enabled + acl_enabled 同开才有意义。
    kb_enabled: bool = True
    quota_per_minute: int = 60  # 租户分钟滑动窗口上限
    quota_per_day: int = 10000  # 租户每日调用上限
    quota_token_per_day: int = 0  # 租户每日 token 消耗上限（0 = 不限制该维度）
    # 平台侧配额（账号级，所有租户共享的上游约束）：0 = 只统计不拦截
    platform_quota_per_day: int = 0  # 账号每日平台调用预算（LLM+Embedding+Rerank）
    platform_quota_warn_ratio: float = 0.8  # 余量低于该比例进入 warning
    sensitive_scan_enabled: bool = True  # 输出侧敏感词扫描

    # ----  工具：业务库 / 联网检索（未配置 DSN 时工具不注册）----
    sql_dsn: str = ""  # 只读业务库 DSN（sqlite/duckdb/postgres），空则不提供 sql_search
    sql_readonly: bool = True  # 强制只读：禁止 INSERT/UPDATE/DELETE/DDL
    sql_max_rows: int = 50
    # 租户隔离列：配置后 sql_search 强制「单表查询 + 表白名单 + 谓词包装」。
    # 空 = 不做租户过滤（仅限单租户业务库，多租户下启用 sql_search 必须配置）
    sql_tenant_column: str = ""
    web_search_endpoint: str = ""  # 联网搜索 HTTP 端点，空则不提供 web_search
    web_search_api_key: str = ""
    web_search_timeout_s: float = 10.0

    # ---- 存储 ----
    data_dir: Path = BASE_DIR / "data"
    # 向量库选型：milvus（生产，standalone / 测试 lite 文件）| faiss（本地文件）
    vector_store: str = "milvus"
    # 注意：环境变量名用 KQUERY_ 前缀——MILVUS_URI 与 pymilvus 内部变量冲突
    milvus_uri: str = Field(default="http://localhost:19530", validation_alias="KQUERY_MILVUS_URI")
    milvus_collection: str = "kquery_chunks"

    # ---- 租户----
    default_tenant_id: str = "default"

    # ---- 云端调用失败模式治理：超时 / 重试（限流退避）----
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3

    # ---- 并发提容（批次 1/2/4，单体进程内机制）----
    # 在飞问答线程数：按"上游并发配额"而不是 CPU 设；真实模型 8s/req 时
    # 32 在飞 ≈ 4 QPS 的承载（Little's Law）。排队满即 503 + Retry-After。
    pipeline_workers: int = 32
    pipeline_queue_size: int = 200
    # Agent 路径独立的并发闸门：每 run 一个执行线程 + 每工具一次池调用
    agent_run_permits: int = 16
    # 上游隔离仓许可数（之和必须 ≤ pipeline_workers，否则线程被等许可占满）
    upstream_llm_permits: int = 12
    upstream_embedding_permits: int = 4
    upstream_rerank_permits: int = 2
    upstream_milvus_permits: int = 8
    # 熔断：window 次窗口内失败率超过 ratio 即打开 open_seconds，半开 half_open 次探测
    upstream_breaker_window: int = 20
    upstream_breaker_fail_ratio: float = 0.5
    upstream_breaker_open_s: float = 30.0
    # 自适应并发（批次 4）：默认关闭，按 AIMD 在 [1, permits] 间伸缩
    upstream_adaptive: bool = False
    # 降级阶梯（批次 4）：默认关闭；开启后按水位收缩改写/CRAG 与低优先级流量
    degrade_ladder_enabled: bool = False
    degrade_upstream_slow_ms: int = 2000

    # ---- 上传约束 ----
    max_upload_mb: int = 50

    # ---- F4 快问（单文档「上传即问」）----
    # TTL 对齐 doc-qa（2 小时）：临时文档只服务当次问答，留太久会占存储。
    quick_ask_ttl_s: int = 7200
    # 同步 MVP 的规模上限：解析在请求内完成，超大文件会拖住请求线程。
    quick_ask_max_pages: int = 20
    quick_ask_max_mb: int = 20

    # ---- F2 语音输入（火山引擎 STS）----
    # 密钥由后端持有，前端只拿 STS token——密钥绝不能进前端产物。
    volc_appid: str = ""
    volc_access_key: str = ""
    # 资源集群（语音识别通用集群）
    volc_speech_cluster: str = "volcengine_input_common"
    # STS token 有效期（秒）
    speech_sts_duration_s: int = 300
    speech_sts_timeout_s: float = 10.0

    # ---- F2 语音转写（后端 ASR 代理）----
    # 浏览器把录音 POST 上来，由后端代持云厂商凭据完成转写。
    # provider：dashscope（百炼原生，qwen3-asr-flash / fun-asr-flash）
    #          | openai（任何兼容 /v1/audio/transcriptions 的服务）
    #          | none（关闭语音转写）
    speech_asr_provider: str = "dashscope"
    speech_asr_model: str = "qwen3-asr-flash"
    # 自定义转写端点（留空 = 用 provider 默认地址）
    speech_asr_base_url: str = ""
    # 转写用 API Key（留空 = 复用 DASHSCOPE_API_KEY）
    speech_asr_api_key: str = ""
    # 单次转写音频上限（MB）与上游超时（秒）
    speech_max_audio_mb: int = 10
    speech_asr_timeout_s: float = 60.0

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def resolved_provider(self) -> str:
        if self.model_provider != "auto":
            return self.model_provider
        return "dashscope" if self.dashscope_api_key else "mock"

    @property
    def resolved_ocr_provider(self) -> str:
        """OCR 实际使用的 provider（只可能是云服务 API 或 mock）。

        `auto` 跟随 `model_provider`；显式指定 `dashscope` 时强制走云端
        （此时必须有平台凭据，否则启动期校验会拦下）。
        """
        if self.ocr_provider != "auto":
            return self.ocr_provider
        return self.resolved_provider

    # ---- 启动期校验（stage-10 §5.1：缺项宁可在 boot 时崩，也不要运行期才暴露）----

    def config_items(self) -> list[dict]:
        """结构化配置声明：驱动启动期校验报告与前端"系统配置"页。

        `required` 的判定依据是"缺了会不会导致功能不可用且不报错"。
        """
        return [
            {"key": "model_provider", "value": self.model_provider, "required": True,
             "is_secret": False, "effective_scope": "process"},
            # OCR 只允许云服务 API（auto | dashscope | mock），没有本地模型选项
            {"key": "ocr_provider", "value": self.ocr_provider, "required": True,
             "is_secret": False, "effective_scope": "process"},
            {"key": "dashscope_api_key", "value": "***" if self.dashscope_api_key else "",
             "required": self.model_provider == "dashscope", "is_secret": True,
             "effective_scope": "process"},
            {"key": "llm_model", "value": self.llm_model, "required": True, "is_secret": False,
             "effective_scope": "request"},
            {"key": "embedding_model", "value": self.embedding_model, "required": True,
             "is_secret": False, "effective_scope": "index"},
            {"key": "rerank_model", "value": self.rerank_model, "required": True,
             "is_secret": False, "effective_scope": "request"},
            {"key": "vector_store", "value": self.vector_store, "required": True,
             "is_secret": False, "effective_scope": "index"},
            {"key": "milvus_uri", "value": self.milvus_uri,
             "required": self.vector_store == "milvus", "is_secret": False,
             "effective_scope": "index"},
            {"key": "data_dir", "value": str(self.data_dir), "required": True, "is_secret": False,
             "effective_scope": "process"},
            {"key": "auth_jwt_secret", "value": "***" if self.auth_jwt_secret else "",
             "required": self.auth_jwt_enabled or self.auth_password_enabled,
             "is_secret": True, "effective_scope": "process"},
            {"key": "auth_password_enabled", "value": self.auth_password_enabled,
             "required": False, "is_secret": False, "effective_scope": "process"},
            {"key": "auth_registration_enabled", "value": self.auth_registration_enabled,
             "required": False, "is_secret": False, "effective_scope": "process"},
            {"key": "auth_session_ttl_s", "value": self.auth_session_ttl_s,
             "required": self.auth_password_enabled, "is_secret": False, "effective_scope": "request"},
            # 明文口令，必须与 auth_jwt_secret 同样掩码——它出现在启动自检输出里
            {"key": "auth_bootstrap_admin_password",
             "value": "***" if self.auth_bootstrap_admin_password else "",
             "required": False, "is_secret": True, "effective_scope": "process"},
            {"key": "default_tenant_id", "value": self.default_tenant_id, "required": True,
             "is_secret": False, "effective_scope": "process"},
            {"key": "cors_origins", "value": self.cors_origins, "required": True,
             "is_secret": False, "effective_scope": "process"},
        ]

    def validate_startup(self) -> list[dict]:
        """返回问题清单；无问题时返回空列表。

        调用方（`create_app`）遇到非空清单必须中止启动并给出可读的诊断，
        而不是打一条 warning 就带着坏配置对外服务。
        """
        issues: list[dict] = []

        def add(field: str, reason: str, hint: str) -> None:
            issues.append({"field": field, "reason": reason, "hint": hint})

        for item in self.config_items():
            if item["required"] and not str(item["value"]).strip():
                add(item["key"], "必填项缺失", "在环境变量或 backend/.env 中显式提供该配置")

        if self.model_provider not in {"auto", "dashscope", "mock"}:
            add("model_provider", f"非法取值 {self.model_provider}", "只能取 auto | dashscope | mock")
        if self.ocr_provider not in {"auto", "dashscope", "mock"}:
            # 本地/自部署 OCR 模型不在允许范围内（架构约束：OCR 一律云服务 API）
            add("ocr_provider", f"非法取值 {self.ocr_provider}",
                "OCR 只支持云服务 API：auto | dashscope | mock；不支持本地/自部署模型")
        if self.ocr_provider == "dashscope" and not self.dashscope_api_key:
            add("dashscope_api_key", "ocr_provider=dashscope 但未提供平台凭证",
                "补齐 KQUERY_DASHSCOPE_API_KEY，或显式设置 ocr_provider=mock")
        if self.model_provider == "dashscope" and not self.dashscope_api_key:
            # 显式要求走平台却没给凭据：这是配置错误，不该静默降级成 mock
            add("dashscope_api_key", "model_provider=dashscope 但未提供平台凭证",
                "补齐 KQUERY_DASHSCOPE_API_KEY，或显式设置 model_provider=mock")
        if self.model_provider == "auto" and not self.dashscope_api_key:
            # auto + 无凭据会静默解析为 mock（resolved_provider），生产漏配变量时
            # 全面接管且仅打 INFO 日志——宁可启动失败，也不要静默裸奔
            add("dashscope_api_key", "model_provider=auto 但未提供平台凭证，将静默降级为 mock",
                "补齐 KQUERY_DASHSCOPE_API_KEY，或显式设置 model_provider=mock 以承认测试环境")
        if self.vector_store not in {"milvus", "faiss"}:
            add("vector_store", f"非法取值 {self.vector_store}", "只能取 milvus | faiss")
        if (self.auth_enabled and not self.auth_jwt_enabled
                and not self.auth_password_enabled and not self.auth_tokens_json.strip()):
            add("auth_tokens_json", "auth_enabled=true 但无任何凭证来源，认证将无人可通过",
                "启用 auth_password_enabled 走账号密码登录，或提供 auth_tokens_json（静态凭证表），"
                "或启用 auth_jwt_enabled 走标准令牌，或显式设置 auth_enabled=false 承认开发/测试模式")
        if (self.auth_jwt_enabled or self.auth_password_enabled) and len(self.auth_jwt_secret) < 16:
            add("auth_jwt_secret", "JWT 密钥过短（<16 字符）",
                "使用至少 32 字节随机串；账号密码登录模式下签发与验签共用这把密钥")
        if self.auth_password_enabled and self.auth_session_ttl_s <= 0:
            add("auth_session_ttl_s", "登录令牌有效期必须为正", "建议 900（15 分钟）")
        if self.auth_password_enabled and self.auth_registration_enabled and self.auth_enabled is False:
            # 不致命：auth_enabled=false 时登录端点本就无法通过认证，此处只避免误配的困惑
            logger.warning("已开启自助注册，但 auth_enabled=false，登录端点不会生效")
        if self.top_k <= 0:
            add("top_k", "top_k 必须为正整数", "推荐 4")
        if self.chunk_overlap >= self.chunk_size:
            add("chunk_overlap", "重叠长度必须小于块长度", "推荐 chunk_size=300 / overlap=50")
        if self.rrf_k <= 0:
            add("rrf_k", "RRF 平滑常数必须为正", "论文默认 60")
        if not 0.0 <= self.cache_sim_threshold <= 1.0:
            add("cache_sim_threshold", "语义缓存阈值必须落在 [0,1]", "推荐 0.95")
        if not 0.0 <= self.parametric_fallback_score_threshold <= 1.0:
            add("parametric_fallback_score_threshold", "兜底判定阈值必须落在 [0,1]",
                "推荐 0.6（与 reflect 的 ANSWER_THRESHOLD 同口径）")
        if not 0.0 <= self.parametric_fallback_confidence <= 1.0:
            add("parametric_fallback_confidence", "兜底置信度必须落在 [0,1]", "推荐 0.3")
        if self.quota_per_minute <= 0:
            add("quota_per_minute", "分钟配额必须为正整数", "推荐 60")
        if self.cache_backend not in {"auto", "memory", "redis"}:
            add("cache_backend", f"非法取值 {self.cache_backend}", "只能取 auto | memory | redis")
        if self.cache_backend == "redis" and not self.redis_url.strip():
            # 显式要求 Redis 模式却没给地址：这是配置错误，不该静默退回内存
            add("redis_url", "cache_backend=redis 但未提供 redis_url",
                "补齐 REDIS_URL，或显式设置 cache_backend=memory 承认单副本语义")
        if self.platform_quota_warn_ratio <= 0 or self.platform_quota_warn_ratio >= 1:
            add("platform_quota_warn_ratio", "告警水位必须落在 (0,1)", "推荐 0.8")
        if issues:
            # 注意：这里**不能**再 `logger = logging.getLogger(__name__)`。
            # 同一函数上方（如自助注册告警）已引用过模块级 logger，Python 见到
            # 局部赋值就会把整个函数里的 logger 当作局部变量，于是前面的引用
            # 变成 UnboundLocalError——只在走到告警分支时才暴露，极难定位。
            detail = "; ".join(f"{i['field']}（{i['reason']}）" for i in issues)
            logger.error("启动期配置校验未通过：%s", detail)
        return issues

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def metadata_file(self) -> Path:
        return self.data_dir / "metadata.json"

    @property
    def users_file(self) -> Path:
        return self.data_dir / "users.json"

    @property
    def knowledge_bases_file(self) -> Path:
        return self.data_dir / "knowledge_bases.json"

    @property
    def kb_grants_file(self) -> Path:
        return self.data_dir / "kb_grants.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
