"""全局配置：阈值、模型名、路径一律外置（设计原则 5：配置外置）。"""
import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from app.core.logging_setup import get_logger

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/ 根目录
# 仓库根（运行时数据目录 data/ 的父目录）：`backend/` 与 `tests/` 的同级目录。
REPO_DIR = BASE_DIR.parent

logger = get_logger(__name__)

# 启动期配置回显的脱敏规则（`A-16`）：字段名含这四类词的，值一律 `***`；
# 连接串里的口令段（`redis://:pwd@host`）同样脱敏。
_SECRET_NAME_RE = re.compile(r"(key|token|secret|password)", re.IGNORECASE)
_DSN_CREDENTIAL_RE = re.compile(r"://([^:/@]*):([^@]*)@")

#: **只有对话补全**的生成 provider：没有 rerank / OCR 端点。
#:
#: 实测修正：**MiniMax 另有 `embo-01` 向量接口**（`POST /v1/embeddings`，
#: 1536 维，形状不是 OpenAI 的——字段是 `texts`/`type`），所以它**不再算**在
#: 「不能做向量」的那一类里；但 rerank / OCR 仍然没有，`auto` 下必须回落 dashscope。
#: DeepSeek 三条都没有（`/v1/embeddings`、`/beta/embeddings`、`/v1/rerank` 全 404）。
#:
#: **智谱（zhipu）不在此列**：`POST /api/paas/v4/embeddings`（`embedding-3`，2048 维，
#: OpenAI 形状）与 `POST /api/paas/v4/rerank`（`results[].relevance_score`）都实测可用，
#: 三条通道齐全，`auto` 下**跟着走**、不回落。
CLOUD_ONLY_GENERATION_PROVIDERS = frozenset({"deepseek", "minimax"})

#: 各 provider 的**向量模型默认名与维度**（只在用户没显式改模型名时生效）。
#:
#: 为什么需要这张表：`embedding_model` / `embedding_dim` 的出厂默认是千问的
#: `text-embedding-v3` / 1024。把 `EMBEDDING_PROVIDER` 换成 `minimax` 而不改这两个时，
#: 首次入库就会 400（模型名不存在）；维度对不上则更糟——**写坏向量库**。
#: 维度必须与 `minimax_store` 的集合 schema 一致，所以模型名与维度在这里成对声明。
EMBEDDING_PROVIDER_DEFAULTS: dict[str, dict] = {
    "minimax": {"model": "embo-01", "dim": 1536},
    # 智谱 `embedding-3`：实测 2048 维、OpenAI 形状（`data[].embedding`）。
    # **维度与 embo-01 的 1536 不同**——换 provider 必须重建向量集合并重新入库。
    "zhipu": {"model": "embedding-3", "dim": 2048},
}

#: 各档位在「非千问」provider 下的默认模型名。
#:
#: 为什么需要这张表：`llm_model` / `intent_model` / `agent_planner_model` 的出厂默认
#: 是千问模型名（`qwen-plus` / `qwen-turbo`）。把 `MODEL_PROVIDER` 换成 deepseek 或
#: minimax 而不同时改这三个模型名时，**首次请求就会 400/404**——而且报错来自上游，
#: 看起来像「key 不对」。这里在解析期做一次替换，让「换 provider」真的只需改一个变量。
#: 用户显式写了非千问模型名时**一律优先用户配置**。
PROVIDER_MODEL_DEFAULTS: dict[str, dict[str, str]] = {
    "deepseek": {
        "llm": "deepseek-chat",
        "intent": "deepseek-chat",
        "agent": "deepseek-chat",
    },
    "minimax": {
        # Flash 档做意图识别（高频、便宜）；M3 做规划（要更强的多步推理）；
        # M3 系原生多模态，图像直答也用 Flash（直答复用同一凭据与端点）
        "llm": "MiniMax-M3.1-Flash",
        "intent": "MiniMax-M3.1-Flash",
        "agent": "MiniMax-M3",
        "vlm": "MiniMax-M3.1-Flash",
    },
    # 智谱（bigmodel.cn，按量计费档）。`glm-4.5` 系**默认开思考**：实测
    # `max_tokens=120` 时思考把预算吃光、`finish_reason=length`、正文为空——
    # 所以 `ZhipuLLM` 默认下发 `thinking: {"type": "disabled"}`（见 `zhipu_thinking_enabled`）。
    "zhipu": {
        "llm": "glm-4.6",
        "intent": "glm-4.5-flash",  # 高频轻量判定，未开思考也不吃预算
        "agent": "glm-4.6",
        "vlm": "glm-4.5v",  # 原生多模态，与文本共用 /chat/completions
    },
}


def _is_qwen_model(name: str) -> bool:
    return str(name or "").strip().lower().startswith("qwen")


def _is_dashscope_embedding_model(name: str) -> bool:
    """千问侧向量模型名（`text-embedding-v1..v4`）。

    不能只用 `_is_qwen_model`：向量模型名**不带** qwen 前缀（`text-embedding-v3`），
    换 provider 时会漏替换，然后首次入库就 400。
    """
    text = str(name or "").strip().lower()
    return text.startswith("qwen") or text.startswith("text-embedding")


def _mask_config_value(name: str, value) -> str:
    """按字段名与内容脱敏一个配置值。"""
    if _SECRET_NAME_RE.search(name):
        return "***" if value not in (None, "") else ""
    text = str(value)
    return _DSN_CREDENTIAL_RE.sub(r"://\1:***@", text)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # ---- 运行模式（dev | test | prod）----
    # prod = 生产：关闭交互式 API 文档（/docs / /redoc / /openapi.json）——
    # 设计项 `A-04`（生产禁用交互式文档）。
    # 必须是**显式配置**：不能用「日志是不是 json」这类隐式信号判断「是不是生产」，
    # 否则「生产忘了配」= 生产照样开着文档。
    app_env: str = "dev"

    # ---- 千问平台（阿里云百炼 / DashScope）----
    dashscope_api_key: str = ""
    # auto: 有 key 走 dashscope，无 key 走 mock（便于无凭据环境下的功能测试）
    model_provider: str = "auto"  # auto | dashscope | deepseek | minimax | zhipu | mock

    # ---- DeepSeek（OpenAI 兼容的对话补全）----
    # **DeepSeek 不提供 embeddings / rerank 端点**（实测 `/v1/embeddings`、`/beta/embeddings`、
    # `/v1/rerank` 全 404）：官方 API 只有 `/chat/completions`。
    # 所以它的 key 只能用于**生成**，或用 `rerank_provider=deepseek` 做
    # **对话模型 listwise 重排**（走 `/chat/completions`，不是 `/rerank`）。
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"

    # ---- MiniMax（OpenAI 兼容的对话补全；M3 系列**原生多模态**）----
    # 端点 `{base}/chat/completions` 与 DeepSeek 同形，所以复用同一个客户端；
    # 两处**只有 MiniMax 才有**的行为必须显式处理，否则会静默失败：
    #   ① 业务错误放在 **HTTP 200 的响应体**里（`base_resp.status_code != 0`），
    #      不检查就会把错误当成功、把空答案当「模型没话说」；
    #   ② 国内站 `api.minimax.chat` 与国际站 `api.minimaxi.com` 是**两套域名**，
    #      凭据不通用，换域名等于换账号。
    # MiniMax **有** `embo-01` 向量接口（`POST {base}/embeddings`，1536 维，
    # 请求体是 `texts`/`type` 而非 OpenAI 的 `input`，见 `services/embeddings.MiniMaxEmbeddings`）；
    # **没有** rerank 端点（实测 404）。
    minimax_api_key: str = ""
    minimax_base_url: str = "https://api.minimax.chat/v1"

    # ---- 智谱（bigmodel.cn，OpenAI 兼容；三条通道齐全）----
    # 实测：
    #   `POST {base}/chat/completions` → glm-4.6 / glm-4.5 / glm-4-air / glm-4-flash 均通；
    #   `POST {base}/embeddings`       → `embedding-3`，**2048 维**，OpenAI 形状；
    #   `POST {base}/rerank`           → `results[].relevance_score`，与 OpenAI 兼容重排同形。
    # **唯一坑**：glm-4.5 系默认开思考，思考 token 计入 `max_tokens`，
    # 预算小时正文为空（`finish_reason=length`）——默认关掉（见 `zhipu_thinking_enabled`）。
    zhipu_api_key: str = ""
    zhipu_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    # 是否保留 glm-4.5 系的思考链。RAG 要事实性短答，思考只是烧钱与延迟；
    # 需要多步推理（Agent 规划复核）时才打开。
    zhipu_thinking_enabled: bool = False

    # ---- 模型路由与降级（`LM-01`）----
    # **默认关**：降级会改变「用哪个模型回答」，必须在明确知道代价时才开。
    llm_fallback_enabled: bool = False
    # 备选模型候选链（逗号分隔，按顺序尝试）。
    # 例：`deepseek-chat,qwen-plus`——主模型失败且**可降级**时依次尝试。
    llm_fallback_models: str = ""

    # ---- 向量化 / 重排的 provider（与生成模型解耦）----
    # auto = 跟随 model_provider（dashscope / minimax）；openai_compatible = 走
    # `*_base_url` 指向的 OpenAI 兼容服务；mock = 本地假实现。
    # embedding 另可显式取 minimax（embo-01，非 OpenAI 形状）；
    # rerank 另可显式取 deepseek（**对话模型做 listwise 重排**，不是 /rerank 端点）。
    # embedding 另可显式取 zhipu（embedding-3，2048 维，OpenAI 形状）；
    # rerank 另可显式取 zhipu（/rerank 端点，results[].relevance_score）。
    embedding_provider: str = "auto"  # auto | dashscope | minimax | zhipu | openai_compatible | mock
    rerank_provider: str = "auto"  # auto | dashscope | deepseek | zhipu | openai_compatible | mock
    # OpenAI 兼容服务的地址（不含路径后缀）：
    # embeddings → {base}/embeddings；rerank → {base}/rerank
    embedding_base_url: str = ""
    rerank_base_url: str = ""
    # 兼容服务的 key（留空则复用 dashscope_api_key）
    embedding_api_key: str = ""
    rerank_api_key: str = ""
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
    # 出厂默认 800/100（`MP-05`）：原 300/50 是「能跑通」的值，
    # 不是量出来的值——按 `doc/analysis/检索策略推荐表.md` 的口径，300 字会把
    # 一个完整论证切断在块边界上，答案只能拼凑。显式配置 `CHUNK_SIZE` 的部署
    # 不受此变更影响（环境变量优先）。
    chunk_size: int = 800
    chunk_overlap: int = 100
    # 切块策略预设（`MP-05` 的准备）：**空 = 沿用上面的 chunk_size / chunk_overlap**
    # （兼容旧行为）；填预设名（如 `recursive-800-100`）则**整体覆盖**三者，
    # 让「改默认切块策略」变成**改一行配置**而不是改代码。
    # 证据与口径见 `doc/analysis/检索策略推荐表.md`。
    chunk_policy: str = ""
    top_k: int = 4
    min_chunk_chars: int = 20  # 超短块标记阈值

    # ---- 向量索引（`A-26`）----
    # 检索"能用"和"能撑"是两件事：FLAT 是全量精确比较，10 万块时每次查询都要
    # 遍历全部向量——召回质量没问题，延迟随库线性上涨。HNSW 用图近似把比较次数
    # 降到对数级，代价是**近似**（极小概率漏掉真邻居，用 `ef_search` 兜）。
    vector_index_type: str = "hnsw"  # hnsw（默认，近似）| flat（精确，小库/对照）
    vector_index_m: int = 32  # 每层邻居数：大 → 召回高、建图慢、内存高
    vector_index_ef_construction: int = 200  # 建图搜索宽度
    vector_index_ef_search: int = 64  # 查询搜索宽度：大 → 准、慢

    # ---- 图谱构建任务（`C-09`）----
    # 全量重建是分钟级操作，必须后台跑；单 worker 串行是关键（图写入要拿全局锁，
    # 并发构建只会拉长锁等待并互相覆盖实体消解表）。
    graph_build_max_pending: int = 4  # 排队上限，超出直接 503
    graph_build_task_retention_s: int = 1800  # 终态任务保留时长

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
    # 本系统标定口径（混合语料实测）：
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
    # 线性问答路径（/ask、/ask/stream）的**请求级**墙钟上限。
    # 此前只有单次 LLM 超时（60s × 3 次重试 + 阻塞退避 ≈183s），整条请求没有上限：
    # 网关会先超时返回 502，而服务端还在烧 token。
    ask_deadline_ms: int = 120000
    # ---- 流式断点续传（`Last-Event-ID`）----
    # 默认**关闭**：开启后生成与连接解耦（泵任务写缓冲），断线不再立刻取消，
    # 而是等 `sse_resume_grace_s` 秒——这段时间照样烧上游 token。这是明确的
    # 「体验 vs 成本」取舍，不该替用户默认打开（`C-04` 的成本纪律优先）。
    # 关闭时行为与之前完全一致，且不占额外内存。
    sse_resume_enabled: bool = False
    # 断开后的宽限期：窗口内带着 `Last-Event-ID` 接上就继续生成，否则取消。
    sse_resume_grace_s: float = 20.0
    # 缓冲存活时间：断线后用户还可能点「继续」，但不会等几分钟。
    sse_replay_ttl_s: float = 120.0
    # /healthz 单项探针超时（秒）：并行探测时每项的上限。
    # 慢依赖不该把整个探针拖到 K8s 的 probe timeout 之外（X-18）。
    health_probe_timeout_s: float = 1.0
    # /metrics 默认需要鉴权（运营数据不该匿名可抓）。Prometheus 侧配 METRICS_TOKEN。
    metrics_public: bool = False
    metrics_token: str = ""
    # 图像向量路（M1）：真跨模态检索（文本查询 → 图像向量空间）。
    # 默认关闭：① 需要多模态 embedding 的 Key；② 图像向量与文本向量**不同空间**，
    # 必须存独立 collection/目录，否则会污染文本检索。
    image_vector_enabled: bool = False
    image_embedding_model: str = "multimodal-embedding-v1"
    # VLM 直答（图文范围，M5）：把**原图**喂给 qwen-vl-max 直接回答。
    # 默认关闭：直答比 caption 文本路贵（一次多模态调用），只在"看图型问题"才值得。
    vlm_answer_enabled: bool = False
    vlm_model: str = "qwen-vl-max"
    # 工具空结果降级映射（JSON）：`{"原工具": "兜底工具"}`。
    # 图谱检索 0 条是**正常业务结果**（库里没这个实体），直接判失败会让 Agent
    # 白丢一次机会；降级到语义检索，命中即算这一步成功。
    agent_tool_fallbacks_json: str = '{"graph_search": "vector_search"}'
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
    # `ST-01` 阶段 B：提示词渲染模式。
    # legacy = 与现行 `PROMPT_TEMPLATE` 逐字节一致（默认）；
    # structured = 五个来源各自成段（记忆段带「供参考，不必盲从」边界声明）。
    # 默认保持 legacy：切换渲染结构会改变答案质量，必须由 judge 前后对比数据驱动，
    # 而不是靠改代码默认值（A/B 产物：`tests/data/eval/partition_ab.json`）。
    context_partition_render: str = "legacy"
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
    # Prometheus 查询 API（/admin/metrics/* 的数据源）。留空 = 读进程内 MetricsStore
    # （单副本本地开发）。多副本部署必须配上，否则看板只反映某一个副本。
    prometheus_url: str = ""
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
    # 认证与授权能力已下线：注册 / 登录 / 口令 / JWT / 静态凭证 /
    # 用户目录，以及文档级 ACL / 知识库授权名单 / 角色门禁 / 权限矩阵。
    # 相关配置项（auth_* / acl_enabled / kb_enabled）一并移除。
    #
    # 保留 `default_tenant_id`：它是**数据隔离维度**（文档归属 / 会话归属 /
    # 缓存键 / 限流分桶），不是权限功能。身份固定见 `app/core/identity.py`。
    # 说明：应用侧配额（分钟/每日/token）、平台侧额度监控与敏感词扫描已下线，
    # 相关配置项与端点一并移除。突发保护仍由令牌桶限流承担（见 services/ratelimit.py）。

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
    data_dir: Path = REPO_DIR / "data"
    # 向量库选型：milvus（Milvus 服务，standalone / 集群）| faiss（本地文件）
    vector_store: str = "milvus"
    # 注意：环境变量名用 KQUERY_ 前缀——MILVUS_URI 与 pymilvus 内部变量冲突
    milvus_uri: str = Field(default="http://localhost:19530", validation_alias="KQUERY_MILVUS_URI")
    milvus_collection: str = "kquery_chunks"
    # 一致性级别：Bounded（默认，延迟更低，写入到可见有毫秒级延迟）|
    # Strong（写入立即可见，代价是每次查询都等最新时间戳）。
    # 只在**新建集合**时生效；既有集合要改需 drop 后由启动流程重建。
    milvus_consistency_level: str = "Bounded"

    # ---- 租户----
    default_tenant_id: str = "default"

    # 输出侧 token 上限（`ST-04`）：**兜底值**——
    # 有预算上下文时按「剩余额度 − 预留」反推；没有预算时用这个值。
    # 0 = 不限制（保持上游默认）。
    llm_max_output_tokens: int = 2000

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
    # 多模态通道（图像向量 / VLM 直答，`C-07`）：此前这条路没有隔离仓，
    # 打满时既不受限也不可见——「看不见的无限并发」。
    upstream_multimodal_permits: int = 2
    # 熔断：window 次窗口内失败率超过 ratio 即打开 open_seconds，半开 half_open 次探测
    upstream_breaker_window: int = 20
    upstream_breaker_fail_ratio: float = 0.5
    upstream_breaker_open_s: float = 30.0
    # 自适应并发（批次 4）：默认关闭，按 AIMD 在 [1, permits] 间伸缩
    upstream_adaptive: bool = False
    # 降级阶梯（批次 4）：默认关闭；开启后按水位收缩改写/CRAG 与低优先级流量
    # 降级阶梯（`C-10`）：默认关闭。**开启前必须先标定阈值**——
    # 误降级的代价（白白少一次改写）比迟降级（多等几秒）大得多。
    degrade_ladder_enabled: bool = False
    # 上游耗时 P95 阈值（毫秒）：超过则进入降级级别 2。
    # 标定依据：正常单次 LLM 调用 3~8s，取实测 P95 的 2 倍 → 8000ms。
    # 旧值 2000ms 偏小，**开着就会一直降级**（把正常调用当成慢调用）。
    degrade_upstream_slow_ms: int = 8000

    # ---- 灰度自动回滚（`OP-02`）----
    # **保守默认：关**。灰度是人工开的，自动回滚也该由人工开——
    # 否则一次指标采集故障就会把正在跑的灰度全撤掉。
    canary_auto_rollback_enabled: bool = False
    # 规则集（JSON 数组）。每条规则：
    # {"metric": "error_rate", "direction": "gt", "threshold": 0.05,
    #  "relative": false, "min_samples": 30, "observe_minutes": 5}
    canary_auto_rollback_rules: str = ""

    # ---- 历史摘要压缩（`ST-02`）----
    # 开关：默认**开启**（本条目标就是上线该能力；评估不通过可回滚开关）。
    history_compaction_enabled: bool = True
    # 触发阈值：历史 token 超过它才压缩（低于它压缩反而多花一次调用）。
    history_compaction_trigger_tokens: int = 4000
    # 保留原文的最近轮数。
    history_compaction_keep_recent: int = 3
    # 摘要本身的输出上限（token）。
    history_compaction_max_output_tokens: int = 600

    # ---- 上传约束 ----
    max_upload_mb: int = 50
    # 低优先级（后台）端点的拒绝阈值：排队深度达到「交互队列上限 × 该比例」时，
    # 后台端点返回 503 + Retry-After（`C-11`）。交互流量始终继续接纳。
    background_reject_queue_ratio: float = 0.8
    # 入库任务在内存里的保留时长（秒，`A-06`）：任务进入终态后保留这么久
    # 供前端查看进度，之后从内存淘汰（否则任务表只增不减）。
    ingest_task_retention_s: int = 1800

    # ---- F4 快问（单文档「上传即问」）----
    # TTL 对齐 doc-qa（2 小时）：临时文档只服务当次问答，留太久会占存储。
    quick_ask_ttl_s: int = 7200
    # 同步 MVP 的规模上限：解析在请求内完成，超大文件会拖住请求线程。
    quick_ask_max_pages: int = 20
    quick_ask_max_mb: int = 20

    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def resolved_provider(self) -> str:
        """实际生效的生成 provider。

        `auto` 的判定顺序：**DeepSeek → MiniMax → 智谱 → 千问**——
        配了对话补全的 key 就用它（用户显式配的凭据优先），
        都没有才回千问，连千问 key 也没有则 mock。
        智谱排在千问之前：智谱三条通道齐全（chat / embeddings / rerank），
        而千问账号欠费时会让 OCR 与重排同时哑掉。
        """
        if self.model_provider != "auto":
            return self.model_provider
        if self.deepseek_api_key:
            return "deepseek"
        if self.minimax_api_key:
            return "minimax"
        if self.zhipu_api_key:
            return "zhipu"
        return "dashscope" if self.dashscope_api_key else "mock"

    @property
    def resolved_llm_api_key(self) -> str:
        """生成模型用的 key（按 provider 取对应凭据）。"""
        if self.resolved_provider == "deepseek":
            return self.deepseek_api_key
        if self.resolved_provider == "minimax":
            return self.minimax_api_key
        if self.resolved_provider == "zhipu":
            return self.zhipu_api_key
        return self.dashscope_api_key

    @property
    def resolved_llm_base_url(self) -> str:
        if self.resolved_provider == "deepseek":
            return self.deepseek_base_url
        if self.resolved_provider == "minimax":
            return self.minimax_base_url
        if self.resolved_provider == "zhipu":
            return self.zhipu_base_url
        return ""

    def _resolve_model_name(self, tier: str, configured: str) -> str:
        """按 provider 解析某一档位的模型名（见 `PROVIDER_MODEL_DEFAULTS`）。

        只替换**千问模型名**：显式配了 `deepseek-chat` / `MiniMax-M3` 这类名字时原样返回，
        否则用户「配了却不生效」比默认值错误更难排查。
        """
        defaults = PROVIDER_MODEL_DEFAULTS.get(self.resolved_provider, {})
        fallback = defaults.get(tier, "")
        if fallback and _is_qwen_model(configured):
            return fallback
        return configured

    @property
    def resolved_llm_model(self) -> str:
        """问答生成档位实际使用的模型名。"""
        return self._resolve_model_name("llm", self.llm_model)

    @property
    def resolved_intent_model(self) -> str:
        """意图识别档位实际使用的模型名。"""
        return self._resolve_model_name("intent", self.intent_model)

    @property
    def resolved_agent_planner_model(self) -> str:
        """Agent 规划 / 图谱抽取档位实际使用的模型名。"""
        return self._resolve_model_name("agent", self.agent_planner_model)

    @property
    def resolved_vlm_model(self) -> str:
        """图像直答档位实际使用的模型名（M3 系原生多模态，与文本共用端点）。"""
        return self._resolve_model_name("vlm", self.vlm_model)

    @property
    def resolved_embedding_provider(self) -> str:
        """向量化 provider。

        - **DeepSeek 没有 `/embeddings`**（实测 404）：`auto` 遇到它时回落 dashscope；
        - **MiniMax 有** `embo-01`（实测可用，1536 维）：`auto` 遇到它时**跟着走**；
        - **智谱有** `embedding-3`（实测可用，2048 维）：`auto` 跟着走；
        - 千问照旧；显式配置的 provider 一律优先。
        """
        if self.embedding_provider != "auto":
            return self.embedding_provider
        provider = self.resolved_provider
        if provider == "deepseek":
            return "dashscope"
        return provider

    @property
    def resolved_embedding_model(self) -> str:
        """向量模型名：默认是千问模型名时，按 provider 换成该 provider 的向量模型。"""
        default = EMBEDDING_PROVIDER_DEFAULTS.get(self.resolved_embedding_provider, {})
        model = default.get("model", "")
        if model and _is_dashscope_embedding_model(self.embedding_model):
            return model
        return self.embedding_model

    @property
    def resolved_embedding_dim(self) -> int:
        """向量维度：模型维度是硬约束（向量库集合 schema 按它建），不跟随上游猜。"""
        if self.resolved_embedding_model == "embo-01" and self.embedding_dim == 1024:
            # 1024 是千问出厂默认；切到 embo-01 时必须同步成 1536，
            # 否则入库时报「维度不符」（比重建索引更晚暴露，代价更大）
            return 1536
        if self.resolved_embedding_model == "embedding-3" and self.embedding_dim == 1024:
            # 智谱 embedding-3 实测 2048 维；同样的道理，1024 是千问默认值而非用户意图
            return 2048
        return self.embedding_dim

    @property
    def resolved_embedding_api_key(self) -> str:
        if self.resolved_embedding_provider == "openai_compatible":
            return self.embedding_api_key or self.dashscope_api_key
        if self.resolved_embedding_provider == "minimax":
            return self.embedding_api_key or self.minimax_api_key
        if self.resolved_embedding_provider == "zhipu":
            return self.embedding_api_key or self.zhipu_api_key
        return self.embedding_api_key or self.dashscope_api_key

    @property
    def resolved_embedding_base_url(self) -> str:
        """向量端点的 base（不含 `/embeddings`）。"""
        provider = self.resolved_embedding_provider
        if provider == "minimax":
            return self.minimax_base_url
        if provider == "zhipu":
            return self.zhipu_base_url
        if provider == "openai_compatible":
            return self.embedding_base_url
        return ""

    @property
    def resolved_rerank_provider(self) -> str:
        """重排 provider。

        `auto` **不**跟随 deepseek / minimax：这两家都没有 `/rerank` 端点，
        而 `deepseek` 的 listwise 重排（走对话模型）要额外一次 LLM 往返 + 成本，
        属于「明确知道代价才开」的能力（`LM-01` 同款理由），必须显式配置。
        **智谱有真 `/rerank` 端点**（实测 `results[].relevance_score`），所以跟随。
        """
        if self.rerank_provider != "auto":
            return self.rerank_provider
        provider = self.resolved_provider
        if provider in CLOUD_ONLY_GENERATION_PROVIDERS:
            return "dashscope"
        return provider

    @property
    def resolved_rerank_model(self) -> str:
        """重排模型名。

        `deepseek` 通道是「对话模型做 listwise 重排」，而 `rerank_model` 的出厂默认
        是千问重排模型名（`gte-rerank-v2`）——原样发过去会 400，所以这里换成对话模型名。
        """
        if self.resolved_rerank_provider == "deepseek" and (
            not str(self.rerank_model).strip() or self.rerank_model.startswith("gte-")
        ):
            defaults = PROVIDER_MODEL_DEFAULTS.get("deepseek", {})
            return defaults.get("llm", "deepseek-chat")
        if self.resolved_rerank_provider == "zhipu" and (
            not str(self.rerank_model).strip() or self.rerank_model.startswith("gte-")
        ):
            # 智谱 /rerank 的 model 字段固定为 `rerank`（实测），千问的 gte-* 会 400
            return "rerank"
        return self.rerank_model

    @property
    def resolved_rerank_api_key(self) -> str:
        provider = self.resolved_rerank_provider
        if provider == "openai_compatible":
            return self.rerank_api_key or self.dashscope_api_key
        if provider == "deepseek":
            return self.rerank_api_key or self.deepseek_api_key
        if provider == "zhipu":
            return self.rerank_api_key or self.zhipu_api_key
        return self.rerank_api_key or self.dashscope_api_key

    @property
    def resolved_rerank_base_url(self) -> str:
        """重排端点的 base（`deepseek` 走 `{base}/chat/completions`）。"""
        provider = self.resolved_rerank_provider
        if provider == "deepseek":
            return self.deepseek_base_url
        if provider == "zhipu":
            return self.zhipu_base_url
        if provider == "openai_compatible":
            return self.rerank_base_url
        return ""

    @property
    def resolved_ocr_provider(self) -> str:
        """OCR 实际使用的 provider（只可能是云服务 API 或 mock）。

        `auto` 跟随 `model_provider`；显式指定 `dashscope` 时强制走云端
        （此时必须有平台凭据，否则启动期校验会拦下）。

        **但生成 provider 是 deepseek / minimax 时不能跟着走**：这两家没有
        OCR 端点，`build_ocr_engine` 收到未知 provider 会**在启动期直接抛错**
        （设计如此——不静默降级）。所以 auto 在这里回落 `dashscope`，
        与向量 / 重排同一条回落规则；没有千问 key 时仍会由启动校验拦下。

        **智谱同样回落**：它有视觉模型（`glm-4.5v`），但那是**对话式**多模态，
        不是 `QwenVlOcr` 的 `/ocr` 协议形状（图片进、纯文本出、带版面清洗）。
        强行复用会把「视觉理解」当「版面 OCR」用，表格结构直接丢——
        所以智谱的视觉能力只走 `vlm_model` 直答通道，OCR 仍需 dashscope 或 mock。
        """
        if self.ocr_provider != "auto":
            return self.ocr_provider
        provider = self.resolved_provider
        # 智谱的视觉是对话式多模态（glm-4.5v），不是 OCR 协议：不回落到它
        if provider in CLOUD_ONLY_GENERATION_PROVIDERS or provider == "zhipu":
            return "dashscope"
        return provider

    # ---- 启动期校验（stage-10 §5.1：缺项宁可在 boot 时崩，也不要运行期才暴露）----

    @property
    def resolved_chunk_policy(self) -> dict:
        """生效的切块策略（`{policy, chunk_size, overlap}`）。

        `chunk_policy` 为空 → 用 `chunk_size` / `chunk_overlap`（旧行为）；
        非空 → 按预设解析（**预设覆盖块大小**）。
        """
        spec = str(self.chunk_policy or "").strip()
        if not spec:
            return {
                "name": "custom",
                # 生产目前只用 recursive（其它策略见 `core/chunk_policies.py` 的预设表）
                "policy": {"strategy": "recursive", "params": {}},
                "chunk_size": int(self.chunk_size),
                "overlap": int(self.chunk_overlap),
            }
        # 按需载入：策略预设表只在用到时导入（避免 core 内部的模块级耦合）
        from app.core.chunk_policies import resolve_policy

        return resolve_policy(spec)

    def effective_summary(self) -> dict:
        """启动期生效配置的**脱敏**摘要（`A-16`）。

        线上出问题时，要翻代码 + 翻环境变量才知道「这次到底用的什么配置」。
        这里给出一行可读摘要：模型与 provider、向量库地址、缓存与限流后端、
        Agent 模式、关键开关。

        ## 脱敏规则

        - 字段名含 `key` / `token` / `secret` / `password` → 值显示 `***`；
        - 连接串里的口令段（`redis://:pwd@host`）→ 同样脱敏。
        """
        summary = {
            "model_provider": self.model_provider,
            "resolved_provider": self.resolved_provider,
            # 三个 provider 各自独立：生成 / 向量 / 重排
            # 「实际用的哪个」必须一眼可见——DeepSeek 没有 embeddings 端点，
            # auto 模式会回落到 dashscope，不看摘要会以为「配了但没生效」。
            "embedding_provider": self.embedding_provider,
            "resolved_embedding_provider": self.resolved_embedding_provider,
            "rerank_provider": self.rerank_provider,
            "resolved_rerank_provider": self.resolved_rerank_provider,
            # 凭据本身脱敏，但**在不在**要看得见：「没配凭据」是最常见的线上问题
            "dashscope_api_key": self.dashscope_api_key,
            "minimax_api_key": self.minimax_api_key,
            "zhipu_api_key": self.zhipu_api_key,
            # 回显**实际生效**的模型名（可能已被 provider 默认替换，见 provider_notes）
            "llm_model": self.resolved_llm_model,
            # 向量模型/维度、重排模型同样回显**实际生效值**：靠这两个数字与向量库
            # 集合 schema 对齐，回显原始配置会掩盖「维度不一致」这类致命错配
            "embedding_model": self.resolved_embedding_model,
            "embedding_dim": self.resolved_embedding_dim,
            "rerank_model": self.resolved_rerank_model,
            "vector_store": self.vector_store,
            "milvus_uri": self.milvus_uri,
            "cache_backend": self.cache_backend,
            "redis_url": self.redis_url,
            "metrics_token": self.metrics_token,
            "web_search_api_key": self.web_search_api_key,
            "agent_mode": self.agent_mode,
            "agent_checkpointer": self.agent_checkpointer,
            "agent_hitl_enabled": self.agent_hitl_enabled,
            "crag_enabled": self.crag_enabled,
            "selfcheck_enabled": self.selfcheck_enabled,
            "graph_enabled": self.graph_enabled,
            "graph_store_backend": self.graph_store_backend,
            "image_vector_enabled": self.image_vector_enabled,
            "vlm_answer_enabled": self.vlm_answer_enabled,
            "otel_enabled": self.otel_enabled,
            "ratelimit_capacity": self.ratelimit_capacity,
            "ratelimit_refill_per_min": self.ratelimit_refill_per_min,
            "pipeline_workers": self.pipeline_workers,
            "context_token_budget": self.context_token_budget,
            "log_format": self.log_format,
        }
        return {k: _mask_config_value(k, v) for k, v in summary.items()}

    def provider_notes(self) -> list[str]:
        """启动期提示（人读）：「配了但没生效」的地方必须说出来。

        典型：`MODEL_PROVIDER=deepseek` 时，DeepSeek **没有 embeddings / rerank 端点**
        （实测 `/embeddings`、`/beta/embeddings`、`/v1/rerank` 全 404），auto 模式会回落 dashscope。
        不提示的话，运维会以为「换了模型，检索也换了」——实际上只有生成换了。
        MiniMax 则**有** `embo-01` 向量（auto 跟着走），只有重排会回落。
        """
        notes: list[str] = []
        if self.resolved_provider in CLOUD_ONLY_GENERATION_PROVIDERS:
            name = "DeepSeek" if self.resolved_provider == "deepseek" else "MiniMax"
            if self.resolved_embedding_provider != self.resolved_provider:
                notes.append(
                    f"生成用 {self.resolved_provider}，但向量服务实际是 {self.resolved_embedding_provider}"
                    f"（{name} 不提供 /embeddings 端点，auto 已回落）"
                )
            if self.resolved_rerank_provider != self.resolved_provider:
                notes.append(
                    f"生成用 {self.resolved_provider}，但重排服务实际是 {self.resolved_rerank_provider}"
                    f"（{name} 不提供 /rerank 端点，auto 已回落；"
                    "想用它的对话模型做重排请显式设 RERANK_PROVIDER=deepseek）"
                )
        # 向量模型/维度被按 provider 替换过：这类差异会直接决定集合 schema，必须说出来
        if self.resolved_embedding_model != self.embedding_model:
            notes.append(
                f"embedding_model={self.embedding_model} 是千问模型名，与向量 provider="
                f"{self.resolved_embedding_provider} 不匹配，已替换为 {self.resolved_embedding_model}"
            )
        if self.resolved_embedding_dim != self.embedding_dim:
            notes.append(
                f"embedding_dim={self.embedding_dim} 与 {self.resolved_embedding_model} 不符，"
                f"已按模型默认改为 {self.resolved_embedding_dim}"
                "（**维度变化必须重建向量库索引并重新入库**）"
            )
        if self.resolved_rerank_provider == "deepseek":
            notes.append(
                f"重排走 DeepSeek **对话模型** listwise 重排（{self.deepseek_base_url.rstrip('/')}"
                "/chat/completions）：每次检索多一次 LLM 往返，候选上限 20"
            )
        if self.resolved_provider == "zhipu":
            notes.append(
                f"生成用智谱 {self.resolved_llm_model}"
                f"（思考链{'已开启' if self.zhipu_thinking_enabled else '已关闭'}："
                "glm-4.5 系默认开思考，思考 token 计入 max_tokens，"
                "预算小时正文为空——RAG 短答场景默认关闭）"
            )
        if self.resolved_ocr_provider == "dashscope" and self.resolved_provider == "zhipu":
            notes.append(
                "OCR 仍走 dashscope：智谱的 glm-4.5v 是对话式多模态，"
                "不是 QwenVlOcr 的 /ocr 协议形状（表格结构会丢），"
                "只想做图像直答请用 VLM 通道"
            )
        # 模型名被按 provider 替换过，必须说出来：否则「配置里写的是 qwen-plus，
        # 实际发出去的是 MiniMax-M3.1-Flash」——查成本 / 查报错时会对不上。
        for tier, configured, effective in (
            ("llm_model", self.llm_model, self.resolved_llm_model),
            ("intent_model", self.intent_model, self.resolved_intent_model),
            ("agent_planner_model", self.agent_planner_model, self.resolved_agent_planner_model),
        ):
            if configured != effective:
                notes.append(
                    f"{tier}={configured} 是千问模型名，与生成 provider="
                    f"{self.resolved_provider} 不匹配，已按 provider 默认替换为 {effective}"
                    "（想固定用别的模型就显式设置该变量）"
                )
        return notes

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
            # DeepSeek：只有对话补全（**没有 embeddings / rerank**）
            {"key": "deepseek_api_key", "value": "***" if self.deepseek_api_key else "",
             "required": self.model_provider == "deepseek", "is_secret": True,
             "effective_scope": "process"},
            {"key": "deepseek_base_url", "value": self.deepseek_base_url, "required": False,
             "is_secret": False, "effective_scope": "process"},
            # MiniMax：同样只有对话补全（**M3 系列原生多模态**，但无 embeddings / rerank）
            {"key": "minimax_api_key", "value": "***" if self.minimax_api_key else "",
             "required": self.model_provider == "minimax", "is_secret": True,
             "effective_scope": "process"},
            {"key": "minimax_base_url", "value": self.minimax_base_url, "required": False,
             "is_secret": False, "effective_scope": "process"},
            # 智谱：三条通道齐全（chat / embeddings / rerank），视觉走对话式多模态
            {"key": "zhipu_api_key", "value": "***" if self.zhipu_api_key else "",
             "required": self.model_provider == "zhipu", "is_secret": True,
             "effective_scope": "process"},
            {"key": "zhipu_base_url", "value": self.zhipu_base_url, "required": False,
             "is_secret": False, "effective_scope": "process"},
            # 模型路由与降级（默认关）
            {"key": "chunk_policy", "value": self.chunk_policy, "required": False,
             "is_secret": False, "effective_scope": "process"},
            {"key": "canary_auto_rollback_enabled", "value": self.canary_auto_rollback_enabled,
             "required": True, "is_secret": False, "effective_scope": "process"},
            {"key": "canary_auto_rollback_rules", "value": self.canary_auto_rollback_rules,
             "required": False, "is_secret": False, "effective_scope": "process"},
            {"key": "history_compaction_enabled", "value": self.history_compaction_enabled,
             "required": True, "is_secret": False, "effective_scope": "process"},
            {"key": "history_compaction_trigger_tokens", "value": self.history_compaction_trigger_tokens,
             "required": True, "is_secret": False, "effective_scope": "process"},
            {"key": "history_compaction_keep_recent", "value": self.history_compaction_keep_recent,
             "required": True, "is_secret": False, "effective_scope": "process"},
            {"key": "llm_max_output_tokens", "value": self.llm_max_output_tokens, "required": True,
             "is_secret": False, "effective_scope": "process"},
            # `ST-01` 阶段 B：提示词渲染模式（legacy | structured）
            {"key": "context_partition_render", "value": self.context_partition_render,
             "required": True, "is_secret": False, "effective_scope": "process"},
            {"key": "llm_fallback_enabled", "value": self.llm_fallback_enabled, "required": True,
             "is_secret": False, "effective_scope": "process"},
            {"key": "llm_fallback_models", "value": self.llm_fallback_models, "required": False,
             "is_secret": False, "effective_scope": "process"},
            # 向量 / 重排的 provider 与 OpenAI 兼容服务地址（与生成模型解耦）
            {"key": "embedding_provider", "value": self.embedding_provider, "required": True,
             "is_secret": False, "effective_scope": "index"},
            {"key": "rerank_provider", "value": self.rerank_provider, "required": True,
             "is_secret": False, "effective_scope": "request"},
            {"key": "embedding_base_url", "value": self.embedding_base_url,
             "required": self.embedding_provider == "openai_compatible",
             "is_secret": False, "effective_scope": "index"},
            {"key": "rerank_base_url", "value": self.rerank_base_url,
             "required": self.rerank_provider == "openai_compatible",
             "is_secret": False, "effective_scope": "request"},
            {"key": "embedding_api_key", "value": "***" if self.embedding_api_key else "",
             "required": False, "is_secret": True, "effective_scope": "index"},
            {"key": "rerank_api_key", "value": "***" if self.rerank_api_key else "",
             "required": False, "is_secret": True, "effective_scope": "request"},
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

        if self.model_provider not in {"auto", "dashscope", "deepseek", "minimax", "zhipu", "mock"}:
            add(
                "model_provider",
                f"非法取值 {self.model_provider}",
                "只能取 auto | dashscope | deepseek | minimax | zhipu | mock",
            )
        if self.embedding_provider not in {
            "auto", "dashscope", "minimax", "zhipu", "openai_compatible", "mock"
        }:
            add(
                "embedding_provider",
                f"非法取值 {self.embedding_provider}",
                "只能取 auto | dashscope | minimax | zhipu | openai_compatible | mock"
                "（DeepSeek 没有 embeddings 端点，不接受 deepseek）",
            )
        if self.rerank_provider not in {
            "auto", "dashscope", "deepseek", "zhipu", "openai_compatible", "mock"
        }:
            add(
                "rerank_provider",
                f"非法取值 {self.rerank_provider}",
                "只能取 auto | dashscope | deepseek | zhipu | openai_compatible | mock"
                "（deepseek = 对话模型做 listwise 重排，不是 /rerank 端点）",
            )
        # 显式选了 provider 却没给对应凭据：启动期就要失败（否则首次检索才发现）
        if self.embedding_provider == "minimax" and not (self.embedding_api_key or self.minimax_api_key):
            add("minimax_api_key", "embedding_provider=minimax 但未提供 MiniMax 凭证",
                "补齐 MINIMAX_API_KEY（或 EMBEDDING_API_KEY），或把 embedding_provider 改回 dashscope")
        if self.embedding_provider == "minimax" and self.embedding_model.startswith("text-embedding"):
            add("embedding_model", f"非法取值 {self.embedding_model}",
                "embedding_provider=minimax 只支持 embo-01（1536 维），千问模型名会被上游 400")
        if self.embedding_provider == "minimax" and self.embedding_dim not in (0, 1536):
            add("embedding_dim", f"非法取值 {self.embedding_dim}",
                "embo-01 固定 1536 维；维度与向量库集合 schema 不一致会写坏索引")
        if self.rerank_provider == "deepseek" and not (self.rerank_api_key or self.deepseek_api_key):
            add("deepseek_api_key", "rerank_provider=deepseek 但未提供 DeepSeek 凭证",
                "补齐 DEEPSEEK_API_KEY（或 RERANK_API_KEY），或把 rerank_provider 改回 dashscope")
        # OpenAI 兼容通道：地址必须是完整 URL（否则 httpx 报的是「协议不支持」这类难读的错）
        for key in ("embedding_base_url", "rerank_base_url"):
            base = getattr(self, key, "")
            if base and not base.startswith(("http://", "https://")):
                add(key, f"非法取值 {base}", "必须以 http:// 或 https:// 开头")
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
        if self.model_provider == "minimax":
            if not self.minimax_api_key:
                add("minimax_api_key", "model_provider=minimax 但未提供平台凭证",
                    "补齐 MINIMAX_API_KEY，或显式设置 model_provider=mock")
            if not str(self.minimax_base_url or "").startswith(("http://", "https://")):
                add("minimax_base_url", f"非法取值 {self.minimax_base_url}",
                    "必须以 http:// 或 https:// 开头，且**含 /v1**（例：https://api.minimax.chat/v1）")
        if self.model_provider == "zhipu":
            if not self.zhipu_api_key:
                add("zhipu_api_key", "model_provider=zhipu 但未提供平台凭证",
                    "补齐 ZHIPU_API_KEY，或显式设置 model_provider=mock")
            if not str(self.zhipu_base_url or "").startswith(("http://", "https://")):
                add("zhipu_base_url", f"非法取值 {self.zhipu_base_url}",
                    "必须以 http:// 或 https:// 开头，且**含 /api/paas/v4**"
                    "（例：https://open.bigmodel.cn/api/paas/v4）")
        # 显式把向量 / 重排交给智谱时同样要有凭据，且维度必须与模型匹配
        if self.embedding_provider == "zhipu" and not (self.embedding_api_key or self.zhipu_api_key):
            add("zhipu_api_key", "embedding_provider=zhipu 但未提供智谱凭证",
                "补齐 ZHIPU_API_KEY（或 EMBEDDING_API_KEY），或把 embedding_provider 改回 auto")
        if self.embedding_provider == "zhipu" and not str(self.embedding_model).startswith("embedding-"):
            add("embedding_model", f"非法取值 {self.embedding_model}",
                "embedding_provider=zhipu 只支持 embedding-3（2048 维）")
        if self.embedding_provider == "zhipu" and self.embedding_dim not in (0, 2048):
            add("embedding_dim", f"非法取值 {self.embedding_dim}",
                "embedding-3 固定 2048 维；与 embo-01（1536）之间切换必须重建集合")
        if self.rerank_provider == "zhipu" and not (self.rerank_api_key or self.zhipu_api_key):
            add("zhipu_api_key", "rerank_provider=zhipu 但未提供智谱凭证",
                "补齐 ZHIPU_API_KEY（或 RERANK_API_KEY），或把 rerank_provider 改回 auto")
        if self.model_provider == "auto" and not (
            self.dashscope_api_key or self.deepseek_api_key
            or self.minimax_api_key or self.zhipu_api_key
        ):
            # auto + 无凭据会静默解析为 mock（resolved_provider），生产漏配变量时
            # 全面接管且仅打 INFO 日志——宁可启动失败，也不要静默裸奔。
            # 三个生成凭据**任意一个**存在都算配好（auto 的判定顺序见 resolved_provider）。
            add("dashscope_api_key",
                "model_provider=auto 但未提供任何生成凭据（dashscope / deepseek / minimax / zhipu），"
                "将静默降级为 mock",
                "补齐 KQUERY_DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / MINIMAX_API_KEY / ZHIPU_API_KEY 之一，"
                "或显式设置 model_provider=mock 以承认测试环境")
        # `LM-03`：**所有上游许可之和 ≤ pipeline_workers**（启动期校验 = 兜底）。
        # 此前这条硬约束只写在注释里，只有 `scripts/capacity.py` 会查——
        # 也就是说：不跑容量脚本的人，配错了也照样启动。
        # 后果是「线程全在等许可」：在飞数看着很低，实际全卡在许可上。
        permit_sum = sum(
            int(getattr(self, f"upstream_{name}_permits", 0) or 0)
            for name in ("llm", "embedding", "rerank", "milvus", "multimodal")
        )
        if permit_sum > self.pipeline_workers:
            add(
                "upstream_*_permits",
                f"各上游许可之和 {permit_sum} > pipeline_workers {self.pipeline_workers}",
                "线程数小于许可和时，线程会全卡在等许可上（在飞数看着很低，实际全阻塞）；"
                "调大 pipeline_workers 或调小某条通道的许可",
            )
        if self.vector_store not in {"milvus", "faiss"}:
            add("vector_store", f"非法取值 {self.vector_store}", "只能取 milvus | faiss")
        if self.milvus_consistency_level not in {"Strong", "Bounded", "Session", "Eventually"}:
            add(
                "milvus_consistency_level",
                f"非法取值 {self.milvus_consistency_level}",
                "只能取 Strong | Bounded | Session | Eventually",
            )
        if self.top_k <= 0:
            add("top_k", "top_k 必须为正整数", "推荐 4")
        if self.chunk_overlap >= self.chunk_size:
            add("chunk_overlap", "重叠长度必须小于块长度", "推荐 chunk_size=800 / overlap=100")
        if str(self.chunk_policy or "").strip():
            # 按需载入：策略预设表只在启动校验时导入
            from app.core.chunk_policies import available_presets, resolve_policy

            try:
                resolve_policy(self.chunk_policy)
            except Exception:  # noqa: BLE001 - 非法预设要在启动时暴露
                add(
                    "chunk_policy",
                    f"非法取值 {self.chunk_policy!r}",
                    f"可用预设：{', '.join(available_presets())}",
                )
        if self.rrf_k <= 0:
            add("rrf_k", "RRF 平滑常数必须为正", "论文默认 60")
        if not 0.0 <= self.cache_sim_threshold <= 1.0:
            add("cache_sim_threshold", "语义缓存阈值必须落在 [0,1]", "推荐 0.95")
        if not 0.0 <= self.parametric_fallback_score_threshold <= 1.0:
            add("parametric_fallback_score_threshold", "兜底判定阈值必须落在 [0,1]",
                "推荐 0.6（与 reflect 的 ANSWER_THRESHOLD 同口径）")
        if not 0.0 <= self.parametric_fallback_confidence <= 1.0:
            add("parametric_fallback_confidence", "兜底置信度必须落在 [0,1]", "推荐 0.3")
        if self.cache_backend not in {"auto", "memory", "redis"}:
            add("cache_backend", f"非法取值 {self.cache_backend}", "只能取 auto | memory | redis")
        if self.context_partition_render not in {"legacy", "structured"}:
            # 拼错的枚举值不能静默退回 legacy：那会让「我打开了结构分区」变成
            # 「其实没打开」，而前后对比数据会被归因错。
            add("context_partition_render", f"非法取值 {self.context_partition_render}",
                "只能取 legacy | structured")
        if self.cache_backend == "redis" and not self.redis_url.strip():
            # 显式要求 Redis 模式却没给地址：这是配置错误，不该静默退回内存
            add("redis_url", "cache_backend=redis 但未提供 redis_url",
                "补齐 REDIS_URL，或显式设置 cache_backend=memory 承认单副本语义")
        if self.ask_deadline_ms <= 0:
            add("ask_deadline_ms", "请求级 deadline 必须为正整数", "推荐 120000（2 分钟）")
        # 超时预算链的**告警**（X-05 / X-20）：
        # 单次调用的最坏耗时（超时 × 尝试次数 + 退避）超过调用方预算时，
        # 重试会被 deadline 挤掉——这是性能问题而不是正确性问题
        # （运行时已经按剩余额度裁剪每次调用的超时），所以只告警不拦启动：
        # 否则默认配置（60s 超时 × 3 次重试 vs 60s Agent deadline）会让服务起不来。
        llm_worst_ms = int(self.llm_timeout_s * 1000 * (self.llm_max_retries + 1))
        llm_worst_ms += int(1000 * sum(2**i for i in range(max(0, self.llm_max_retries - 1))))
        if self.agent_deadline_ms and llm_worst_ms > self.agent_deadline_ms:
            logger.warning(
                "单次 LLM 最坏耗时 %dms 超过 Agent deadline %dms：重试会被预算挤掉"
                "（运行时按剩余额度裁剪超时，不会超预算，但可能少一次重试）",
                llm_worst_ms, self.agent_deadline_ms,
            )
        if self.ask_deadline_ms and llm_worst_ms > self.ask_deadline_ms:
            logger.warning(
                "单次 LLM 最坏耗时 %dms 超过请求 deadline %dms：重试会被预算挤掉"
                "（运行时按剩余额度裁剪超时，不会超预算，但可能少一次重试）",
                llm_worst_ms, self.ask_deadline_ms,
            )
        if issues:
            # 注意：这里**不能**再 `logger = get_logger(__name__)`。
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
        """用户目录路径。

        **认证已下线**：用户目录不再被创建或读写，此属性仅供遗留脚本/文档引用，
        新的代码不应使用它。
        """
        return self.data_dir / "users.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
