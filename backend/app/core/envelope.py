"""统一响应信封与错误码。

所有非流式接口返回统一信封，业务数据放在 data 字段内；
错误码分区段，前端按区段映射用户可读文案，不直接展示后端 message。
"""
from enum import IntEnum
from typing import Any

from pydantic import BaseModel


class ErrCode(IntEnum):
    """错误码区段（）：1xxx 认证 / 2xxx 输入 / 3xxx 资源 / 4xxx 检索生成 / 5xxx 服务端 / 6xxx 限流配额 / 8xxx 报告。

    编号全局唯一——IntEnum 中重复数值会产生别名，导致"按码分流"失效
    （曾出现 TASK_NOT_FOUND 与 ASSET_NOT_FOUND 同为 3301 的事故）。

    降级类语义走 `warn_envelope`（status=warn + error.code），不是异常：
    - 44xx 重排降级：前端给"本次未进行精排"的轻提示；
    - 45xx 缓存不可用：**无感降级**，只上报指标不回传用户。
    """

    # ---- 1xxx 认证与权限----
    # 认证与授权已下线：1001/1201/1301 随认证能力一并删除。
    # 1101 保留——`/metrics` 的 X-Metrics-Token 校验仍在用。
    INVALID_REQUEST = 1004  # 请求参数不合法（评测集缺字段等）
    UNAUTHORIZED = 1101  # /metrics 令牌缺失或不匹配

    # ---- 2xxx 输入校验 ----
    INVALID_INPUT = 2001
    QUESTION_EMPTY = 2002
    UNSUPPORTED_FILE = 2003  # 旧码，保留兼容；新入库走 31xx
    FILE_TOO_LARGE = 2004
    # 同一请求正在处理中（幂等在飞去重命中）：不排队、不重复计费，
    # 调用方应等前一个结果或稍后重试。
    REQUEST_IN_PROGRESS = 2005

    # ---- 3xxx 资源 ----
    INDEX_NOT_READY = 3001  # 索引为空：需要检索但无任何可检索内容
    DOCUMENT_NOT_FOUND = 3002
    DOCUMENT_PARSE_FAILED = 3003
    QUICK_DOC_EXPIRED = 3004  # 快问临时文档已过期或已被删除（TTL 到期 / 主动删）

    # ---- 31xx~38xx 入库与解析----
    UNSUPPORTED_FILE_TYPE = 3101  # 文件格式不支持（向导已拦截，此处兜底）
    PARSE_FAILED = 3201  # 解析失败（损坏/加密/OCR 失败），可单文件重试或跳过
    PARSE_ENCRYPTED = 3202  # 文件加密，需人工解密后重传
    TASK_NOT_FOUND = 3301
    ASSET_NOT_FOUND = 3302  # 图片/媒体资源不存在（前端降级占位，不整卡失败）
    ENTITY_NOT_FOUND = 3401  # 实体未在图谱中（降级为纯文本检索）
    SUBGRAPH_TOO_LARGE = 3501  # 子图过大被截断
    CHUNK_POLICY_INVALID = 3601  # 切块参数不合法（向导第 3 步应已拦截）
    INDEX_BUILD_FAILED = 3701  # 索引构建失败，整批重试不可跳过
    VISION_UNAVAILABLE = 3801  # 图片解析不可用


    # ---- 4xxx 检索与生成----
    RETRIEVAL_FAILED = 4001
    GENERATION_FAILED = 4002
    GENERATION_TIMEOUT = 4003

    # 41xx/42xx：Agent 编排
    AGENT_ITERATION_LIMIT = 4101  # 达到迭代上限（"不完整的成功"，仍走成功码）
    TOOL_EXECUTION_FAILED = 4201

    # 43xx：会话
    SESSION_NOT_FOUND = 4301  # 会话不存在或归属不符
    SESSION_EXPIRED = 4302  # 会话已过期（前端提示"已为你开启新对话"）
    # 客户端已断开（`C-04`）：管线在阶段边界提前中止。
    # 为什么给错误码：调用方需要能把它从「服务端 5xx」里区分出来——这类请求
    # 不是故障，恰恰相反，中止才是正确行为（少烧上游 token）。HTTP 用 499
    # （nginx 约定的「客户端关闭请求」），不计入错误率。
    REQUEST_CANCELLED = 4303
    # 流式记录不可续传（`Last-Event-ID` 断点续传）：缓冲已过期、被挤出，
    # 或该帧从未留底（单条答案超过缓冲上限）。
    # 语义是「这次续不上，请重新提问」——与「服务坏了」不同，前端不该自动重试。
    STREAM_NOT_RESUMABLE = 4304

    # 44xx/45xx：降级语义
    RERANK_DEGRADED = 4401  # 重排服务不可用（warn 信封，仍返回结果）
    # 通用知识兜底：知识库未收录相关内容，答案来自模型自身知识（warn 信封，仍返回结果）
    PARAMETRIC_FALLBACK = 4402
    CACHE_UNAVAILABLE = 4501  # 缓存服务不可用（无感降级，只上报指标）

    # 46xx/47xx/48xx：Query 预处理
    INTENT_FAILED = 4601  # 意图识别失败 → 静默降级为"需要检索"
    REWRITE_FAILED = 4701  # 查询改写全部失败 → 降级为原始查询
    DECOMPOSE_FAILED = 4801  # 拆解失败 → 降级为单跳

    # 49xx：校验链路。文档里的"4Axx"无十进制表示，语义映射到 4902。
    SCORER_UNAVAILABLE = 4901  # 平台重排/评分接口不可用（限流或超时）
    SELFCHECK_UNAVAILABLE = 4902  # 幻觉自检不可用

    # ---- 5xxx 服务端 ----
    INTERNAL_ERROR = 5001
    DEPENDENCY_UNAVAILABLE = 5201  # 关键依赖不可用（通用兜底）

    # 52xx：依赖分项（`/healthz` 逐依赖错误码）。
    # 为什么分项而不是共用 5201：调用方要按「哪一类依赖坏了」差异化处置——
    # 向量库挂了该重建索引，共享状态挂了该摘流，模型挂了该降级。
    # 一个码只能让人知道「坏了」，分项码才能让人知道「该做什么」。
    VECTOR_STORE_UNAVAILABLE = 5202  # 向量库 / 文本索引不可用
    GRAPH_STORE_UNAVAILABLE = 5203  # 图存储不可用
    SHARED_STATE_UNAVAILABLE = 5204  # 共享状态（Redis）不可用
    MEMORY_UNAVAILABLE = 5205  # 长期记忆不可用
    PARSER_UNAVAILABLE = 5206  # 解析器不可用
    PROVIDER_UNAVAILABLE = 5207  # 模型提供方不可用
    UPSTREAM_UNAVAILABLE = 5208  # 上游调用面（许可 / 熔断）不可用
    STORAGE_CORRUPTED = 5209  # 真相源损坏（存储文件损坏 / 校验失败）→ 拒绝启动
    CONFIG_INVALID = 5210  # 配置非法 → 改配置重启（500，不是请求参数问题）
    # 服务**不提供该端点**（404/405）：与「服务挂了」是两回事——
    # 例如把 embeddings 指向 DeepSeek（它只有 /chat/completions），
    # 重试多少次都不会成功，必须立刻说清楚「这个服务没这个能力」。
    EMBEDDING_ENDPOINT_UNSUPPORTED = 5211
    RERANK_ENDPOINT_UNSUPPORTED = 5212

    # ---- 6xxx 限流 ----
    RATE_LIMITED = 6101  # 触发突发限流：黄色横幅 + Retry-After 倒计时

    @classmethod
    def is_degraded_code(cls, code: int) -> bool:
        """降级类错误码：走 warn 信封，不阻断结果。"""
        return code in {
            int(cls.RERANK_DEGRADED),
            int(cls.PARAMETRIC_FALLBACK),
            int(cls.CACHE_UNAVAILABLE),
            int(cls.SCORER_UNAVAILABLE),
            int(cls.SELFCHECK_UNAVAILABLE),
            # 46xx~48xx：查询预处理各步失败一律**降级到最朴素路径**（见
            # `services/query/orchestrator.py` 的模块说明：这条链路上没有
            # 「因为预处理失败而中断请求」的分支）。既然不中断，就不该用 error
            # 信封——否则前端会把「本次没做意图识别」显示成「请求失败」。
            int(cls.INTENT_FAILED),
            int(cls.REWRITE_FAILED),
            int(cls.DECOMPOSE_FAILED),
        }


# ---- 错误码 → 类别（`A-09` 的 `kind` 标签）----
# 为什么按**码段**而不是逐码枚举：码段是稳定契约（1xxx~6xxx 各有语义分区），
# 新增码时自动落到正确类别，不需要每次都记得补一张表——
# 「各处各写一份映射表」必然漂移（`A-09` 实现要点 4）。
CODE_KIND_RANGES: tuple[tuple[int, int, str], ...] = (
    (1000, 1999, "admission"),   # 准入类（认证已下线，只剩 /metrics 令牌校验）
    (2000, 2999, "input"),       # 输入类
    (3000, 3999, "resource"),    # 资源类
    (4000, 4999, "upstream"),    # 上游类（含 44xx~49xx 降级告警）
    (5000, 5999, "internal"),    # 内部类
    (6000, 6999, "ratelimit"),   # 限流类
)

# 6 类基数，供指标与测试共用（不要在别处再写一遍数字）
CODE_KINDS: tuple[str, ...] = tuple(kind for _, _, kind in CODE_KIND_RANGES)


def kind_of(code: int) -> str:
    """错误码 → 类别（admission / input / resource / upstream / internal / ratelimit）。

    未知码段归 `internal`：宁可把「不认识的东西」算成内部问题，
    也不要凭空造一个新类别（指标基数会跟着涨）。
    """
    value = int(code)
    for low, high, kind in CODE_KIND_RANGES:
        if low <= value <= high:
            return kind
    return "internal"


# ---- 保留码：没有生产抛出点，但**有意保留**（`A-10` 裁决）----
# 纪律：每个错误码要么有生产引用，要么在此登记「为何保留」。
# 没有这两者之一的码就是「死位」——枚举里占着一个号，前端却永远收不到，
# 按码分流会留下一条死分支。
RETAINED_CODES: dict[ErrCode, str] = {
    ErrCode.INDEX_NOT_READY: (
        "索引为空时不报错，而是以「无内容拒答」（refusal_reason=no_content）表达。"
        "理由：全新部署索引必然为空，若此时报错，用户连第一份文档都传不上去"
        "（与 /readyz「空索引不算未就绪」是同一条纪律）。"
    ),
    ErrCode.SUBGRAPH_TOO_LARGE: (
        "子图过大已被「截断标志 + 丢弃原因」取代（retrieval_stats.truncated / "
        "dropped_reasons），不再作为独立错误码抛出；保留枚举仅为兼容旧数据。"
    ),
    ErrCode.AGENT_ITERATION_LIMIT: (
        "达到迭代上限是「不完整的成功」，仍走成功码（信封 status=ok + "
        "fuse 原因说明），刻意不抛错——抛错会让一次有价值的部分结果整体丢失。"
    ),
}


def validate_code_uniqueness() -> None:
    """自检：IntEnum 重复数值会产生别名，按码分流失效。启动时调用一次。"""
    by_value: dict[int, str] = {}
    for member in ErrCode:
        if member.value in by_value:
            raise RuntimeError(
                f"ErrCode 数值重复：{by_value[member.value]} 与 {member.name} 同为 {member.value}"
            )
        by_value[member.value] = member.name


class ErrorBody(BaseModel):
    code: int
    message: str


class Meta(BaseModel):
    trace_id: str
    elapsed_ms: int


class Envelope(BaseModel):
    status: str  # ok | warn | error
    data: Any = None
    error: ErrorBody | None = None
    meta: Meta


def ok_envelope(data: Any, trace_id: str, elapsed_ms: int) -> Envelope:
    return Envelope(status="ok", data=data, meta=Meta(trace_id=trace_id, elapsed_ms=elapsed_ms))


def warn_envelope(
    data: Any,
    message: str,
    trace_id: str,
    elapsed_ms: int,
    code: int = 0,
) -> Envelope:
    """warn = 部分成功 + 部分告警，前端正常渲染并附加提示。

    `code` 传降级类错误码（44xx 重排 / 45xx 缓存 / 49xx 评分），前端据此映射文案；
    为 0 表示"有告警但无固定码"的软提示。永远不用它表达失败。
    """
    return Envelope(
        status="warn",
        data=data,
        error=ErrorBody(code=code, message=message),
        meta=Meta(trace_id=trace_id, elapsed_ms=elapsed_ms),
    )


def error_envelope(code: ErrCode, message: str, trace_id: str, elapsed_ms: int) -> Envelope:
    return Envelope(
        status="error",
        error=ErrorBody(code=int(code), message=message),
        meta=Meta(trace_id=trace_id, elapsed_ms=elapsed_ms),
    )


class ApiError(Exception):
    """业务异常：携带错误码与 HTTP 状态，由全局异常处理器转成统一信封。"""

    def __init__(
        self,
        code: ErrCode,
        message: str,
        http_status: int = 400,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retry_after = retry_after  # 限流时显式携带，前端展示倒计时
