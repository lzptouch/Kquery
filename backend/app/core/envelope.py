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
    AUTH_FAILED = 1001  # 凭证校验不通过
    INVALID_REQUEST = 1004  # 请求参数不合法（评测集缺字段等）
    UNAUTHORIZED = 1101  # 未认证或凭证过期
    FORBIDDEN = 1201  # 无权限（统一提示"无权限或不存在"，不区分二者防探测）
    ACL_REVOKED = 1301  # 权限被收回

    # ---- 2xxx 输入校验 ----
    INVALID_INPUT = 2001
    QUESTION_EMPTY = 2002
    UNSUPPORTED_FILE = 2003  # 旧码，保留兼容；新入库走 31xx
    FILE_TOO_LARGE = 2004

    # ---- 3xxx 资源 ----
    INDEX_NOT_READY = 3001  # 索引为空：需要检索但无任何可检索内容
    DOCUMENT_NOT_FOUND = 3002
    DOCUMENT_PARSE_FAILED = 3003

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
    AUDIO_TRANSCRIBE_FAILED = 3802  # 音频转写不可用

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
    DEPENDENCY_UNAVAILABLE = 5201  # 关键依赖不可用

    # ---- 6xxx 限流与配额 ----
    RATE_LIMITED = 6101  # 触发租户配额：黄色横幅 + Retry-After 倒计时
    QUOTA_EXCEEDED = 6201  # 平台/每日配额不足：运维事件

    @classmethod
    def is_degraded_code(cls, code: int) -> bool:
        """降级类错误码：走 warn 信封，不阻断结果。"""
        return code in {
            int(cls.RERANK_DEGRADED),
            int(cls.PARAMETRIC_FALLBACK),
            int(cls.CACHE_UNAVAILABLE),
            int(cls.SCORER_UNAVAILABLE),
            int(cls.SELFCHECK_UNAVAILABLE),
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
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retry_after = retry_after  # 限流时显式携带，前端展示倒计时
