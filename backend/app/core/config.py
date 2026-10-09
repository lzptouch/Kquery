"""全局配置：阈值、模型名、路径一律外置（设计原则 5：配置外置）。"""
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/ 根目录


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
    llm_model: str = "qwen-plus"
    embedding_model: str = "text-embedding-v3"
    embedding_dim: int = 1024
    temperature: float = 0.0  # RAG 场景固定 0，事实性优先

    # ---- RAG 参数（阶段 1 推荐值，见 stage-1 README §2.3）----
    chunk_size: int = 300
    chunk_overlap: int = 50
    top_k: int = 4
    min_chunk_chars: int = 20  # 超短块标记阈值

    # ---- 阶段 2：混合检索（stage-2 README §2.1 / §7）----
    retrieval_per_route_top_n: int = 20  # 单路召回条数（最终 Top-K 的 3~5 倍）
    retrieval_fused_top_n: int = 20  # 融合后进入重排的候选数
    rrf_k: int = 60  # RRF 平滑常数（论文默认值）
    bm25_k1: float = 1.5  # 词频饱和
    bm25_b: float = 0.75  # 长度归一
    rerank_model: str = "gte-rerank-v2"

    # ---- 阶段 2：缓存 / 限流 ----
    cache_ttl_s: int = 3600  # 语义缓存 TTL（1 小时）
    # 语义近似的相似度下限：问题向量余弦 ≥ 该值即视为"同一个问题"，
    # 从而让"保修政策 vs 保修条款"这类改写也能命中（stage-2 §2.2 语义级缓存）。
    cache_sim_threshold: float = 0.95
    cache_max_scan: int = 2000  # 近似检索每请求最多比对的候选条目数
    ratelimit_capacity: int = 60  # 令牌桶容量（突发峰值）
    ratelimit_refill_per_min: float = 60.0  # 令牌补充速率（个/分钟，按租户）
    retrieval_config_apply_delay_s: int = 300  # 检索参数变更延迟生效（秒）

    # ---- 阶段 3 增强：扫描版 PDF 自动 OCR ----
    pdf_ocr_enabled: bool = True  # 扫描页自动转 OCR（按页混合策略）
    pdf_ocr_dpi: int = 120  # 页面渲染分辨率（VL 模型识别质量与成本的平衡点）
    pdf_ocr_max_pages: int = 200  # 单文档 OCR 页数上限（成本护栏）
    pdf_scan_min_chars: int = 30  # 单页文本层字符数低于此值判定为扫描页
    # 索引版本化：重新解析生成新版本而非覆盖，历史块保留供引用溯源
    index_version_keep: int = 3  # 保留的历史索引版本数（不含当前版；超出后按版本号从旧到新下线）

    # ---- 阶段 3：音频转写 ----
    transcribe_model: str = "paraformer-v2"  # 文档强制基线（千问平台，不引本地模型）
    audio_max_chunk_s: float = 600.0  # 单次转写提交的最长时长（超出切分后并行提交）
    audio_min_sample_rate: int = 16000  # 低于此采样率标"转写质量可能不佳"
    audio_split_on_silence: bool = True  # 切分点优先落在静音段，避免切断词
    audio_max_workers: int = 4  # 分片并行上限：再高只会加剧平台限流
    # 业务代码版本（阶段 9 §6.3：评测必须同时记录模型版本与代码版本）
    code_version: str = ""  # 留空则评测侧读 KQUERY_CODE_VERSION → git 短 SHA → unknown

    # ---- 阶段 4：Query 预处理 ----
    preprocess_enabled: bool = True
    intent_model: str = "qwen-turbo"  # 意图识别：高频轻量判定压到最低档（成本控制）
    query_expanders: str = "fusion,stepback"  # 启用的改写器（hyde 与 fusion 互斥，不同时启用）
    fusion_n: int = 3  # RAG-Fusion 改写数量（经验最优点）
    query_budget: int = 6  # 单次提问的检索查询预算（超出截断并标记未使用）

    # ---- 阶段 5：CRAG 校验与幻觉自检 ----
    crag_enabled: bool = True
    # 阈值必须按自有评测集的分数分布标定（stage-5 §3.2 的红线）。
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

    # ---- 阶段 6：Agentic RAG ----
    agent_enabled: bool = True  # /ask 带 session_id 或 mode=agent 时走状态机
    agent_max_steps: int = 6  # 单次规划最大步骤数
    agent_planner_model: str = "qwen-plus"  # 规划/反思档位（过低会规划出无效步骤）
    agent_session_ttl_s: int = 30 * 86400  # 会话快照 30 天自动过期
    agent_tool_timeout_s: int = 30  # 单步工具超时（第三层熔断）
    agent_no_progress_limit: int = 3  # 无效进展熔断窗口

    # ---- 阶段 7：GraphRAG 与多模态 ----
    graph_enabled: bool = True
    entity_sim_threshold: float = 0.85  # 实体消歧向量相似度阈值（经验值，需按数据调优）
    graph_bfs_depth: int = 2  # 遍历深度（≥3 噪声剧增）
    graph_seed_top_k: int = 3  # 种子实体上限（防子图爆炸）
    community_max: int = 20  # 社区摘要数量上限（成本控制）
    # 社区层级数：1 = 单层连通分量（默认，成本最低）；
    # >1 逐级重叠合并出粗粒度主题，摘要成本随层级线性增长（stage-7 §5.2/§5.3）
    community_levels: int = 1
    # 层级合并阈值：成员重叠比例 ≥ 该值才合并（调低更粗、调高更细）
    community_merge_threshold: float = 0.3
    image_caption_model: str = "qwen-vl-max"  # 图像描述模型（换版本需全量重编码）

    # ---- 阶段 8：多智能体报告 ----
    multiagent_enabled: bool = True
    writer_model: str = "qwen-max"  # 报告撰写（长文组织对档位最敏感；不可用自动降级）
    critic_model: str = "qwen-plus"  # 评审（多轮循环下档位差异被轮次放大，平衡成本）
    critic_pass_score: float = 8.0  # 通过阈值（0-10）
    critic_max_rounds: int = 3  # 第 3 轮后问题通常在数据层而非写作层
    report_max_subtasks: int = 8

    # ---- 阶段 9：企业护栏 ----
    auth_enabled: bool = False  # 开启后要求 Bearer Token（静态凭证表，仅演示）
    auth_tokens_json: str = ""  # {"token": {user_id, tenant_id, roles, groups}}
    auth_users_json: str = ""  # 用户目录 JSON；留空则读写 data/users.json（持久化）
    # 标准令牌档（企业生产）：HS256 JWT，带签发方/受众校验
    auth_jwt_enabled: bool = False
    auth_jwt_secret: str = ""  # HS256 签名密钥（生产应由 Secret 注入）
    auth_jwt_issuer: str = ""
    auth_jwt_audience: str = ""
    acl_enabled: bool = True  # 文档级 ACL（检索前租户过滤 + 检索后 ACL 双保险）
    quota_per_minute: int = 60  # 租户分钟滑动窗口上限
    quota_per_day: int = 10000  # 租户每日调用上限
    quota_token_per_day: int = 0  # 租户每日 token 消耗上限（0 = 不限制该维度）
    # 平台侧配额（账号级，所有租户共享的上游约束）：0 = 只统计不拦截
    platform_quota_per_day: int = 0  # 账号每日平台调用预算（LLM+Embedding+Rerank）
    platform_quota_warn_ratio: float = 0.8  # 余量低于该比例进入 warning
    sensitive_scan_enabled: bool = True  # 输出侧敏感词扫描

    # ---- 阶段 6 工具：业务库 / 联网检索（未配置 DSN 时工具不注册）----
    sql_dsn: str = ""  # 只读业务库 DSN（sqlite/duckdb/postgres），空则不提供 sql_search
    sql_readonly: bool = True  # 强制只读：禁止 INSERT/UPDATE/DELETE/DDL
    sql_max_rows: int = 50
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

    # ---- 租户（阶段 1 单租户默认值，字段从第一天就存在）----
    default_tenant_id: str = "default"

    # ---- 云端调用失败模式治理：超时 / 重试（限流退避）----
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3

    # ---- 上传约束 ----
    max_upload_mb: int = 50

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def resolved_provider(self) -> str:
        if self.model_provider != "auto":
            return self.model_provider
        return "dashscope" if self.dashscope_api_key else "mock"

    # ---- 启动期校验（stage-10 §5.1：缺项宁可在 boot 时崩，也不要运行期才暴露）----

    def config_items(self) -> list[dict]:
        """结构化配置声明：驱动启动期校验报告与前端"系统配置"页。

        `required` 的判定依据是"缺了会不会导致功能不可用且不报错"。
        """
        return [
            {"key": "model_provider", "value": self.model_provider, "required": True,
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
             "required": self.auth_jwt_enabled, "is_secret": True, "effective_scope": "process"},
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
        if self.model_provider == "dashscope" and not self.dashscope_api_key:
            # 显式要求走平台却没给凭据：这是配置错误，不该静默降级成 mock
            add("dashscope_api_key", "model_provider=dashscope 但未提供平台凭证",
                "补齐 KQUERY_DASHSCOPE_API_KEY，或显式设置 model_provider=mock")
        if self.vector_store not in {"milvus", "faiss"}:
            add("vector_store", f"非法取值 {self.vector_store}", "只能取 milvus | faiss")
        if self.auth_jwt_enabled and len(self.auth_jwt_secret) < 16:
            add("auth_jwt_secret", "JWT 密钥过短（<16 字符）", "使用至少 32 字节随机串")
        if self.top_k <= 0:
            add("top_k", "top_k 必须为正整数", "推荐 4")
        if self.chunk_overlap >= self.chunk_size:
            add("chunk_overlap", "重叠长度必须小于块长度", "推荐 chunk_size=300 / overlap=50")
        if self.rrf_k <= 0:
            add("rrf_k", "RRF 平滑常数必须为正", "论文默认 60")
        if not 0.0 <= self.cache_sim_threshold <= 1.0:
            add("cache_sim_threshold", "语义缓存阈值必须落在 [0,1]", "推荐 0.95")
        if self.quota_per_minute <= 0:
            add("quota_per_minute", "分钟配额必须为正整数", "推荐 60")
        if self.platform_quota_warn_ratio <= 0 or self.platform_quota_warn_ratio >= 1:
            add("platform_quota_warn_ratio", "告警水位必须落在 (0,1)", "推荐 0.8")
        if issues:
            logger = logging.getLogger(__name__)
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
