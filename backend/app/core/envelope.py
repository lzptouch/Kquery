"""统一响应信封与错误码（阶段 0 前端规范 §7.1 / §7.3）。

所有非流式接口返回统一信封，业务数据放在 data 字段内；
错误码分区段，前端按区段映射用户可读文案，不直接展示后端 message。
"""
from enum import IntEnum
from typing import Any

from pydantic import BaseModel


class ErrCode(IntEnum):
    """错误码区段：1xxx 认证 / 2xxx 输入 / 3xxx 资源 / 4xxx 检索生成 / 5xxx 服务端 / 6xxx 限流配额。"""

    AUTH_FAILED = 1001

    INVALID_INPUT = 2001
    QUESTION_EMPTY = 2002
    UNSUPPORTED_FILE = 2003
    FILE_TOO_LARGE = 2004

    INDEX_NOT_READY = 3001
    DOCUMENT_NOT_FOUND = 3002
    DOCUMENT_PARSE_FAILED = 3003
    TASK_NOT_FOUND = 3301
    PARSE_FAILED = 3201  # 解析失败（损坏/加密/OCR 失败），可单文件重试或跳过
    CHUNK_POLICY_INVALID = 3601  # 切块参数不合法（向导第 3 步应已拦截）
    INDEX_BUILD_FAILED = 3701  # 索引构建失败，整批重试不可跳过

    RETRIEVAL_FAILED = 4001
    GENERATION_FAILED = 4002
    GENERATION_TIMEOUT = 4003
    RERANK_DEGRADED = 4401  # 重排服务不可用（降级提示，仍返回结果）

    # 43xx：会话（阶段 6）
    SESSION_NOT_FOUND = 4301  # 会话不存在或归属不符
    SESSION_EXPIRED = 4302  # 会话已过期（前端提示"已为你开启新对话"）

    # 33xx/34xx：多模态与图谱（阶段 7）
    ASSET_NOT_FOUND = 3301  # 图片资源不存在（前端降级占位，不整卡失败）
    ENTITY_NOT_FOUND = 3401  # 实体未在图谱中（降级为纯文本检索）

    # 11xx~13xx：认证与权限（阶段 9）
    UNAUTHORIZED = 1101  # 未认证或凭证过期
    FORBIDDEN = 1201  # 无权限（统一提示"无权限或不存在"，不区分二者防探测）
    ACL_REVOKED = 1301  # 权限被收回

    # 81xx~84xx：多智能体报告（阶段 8）
    REPORT_PLAN_FAILED = 8101  # 任务拆解失败（提供重试与降级普通问答入口）
    REPORT_TASKS_ALL_FAILED = 8201  # 子任务全部失败：不产出空报告（硬约束）
    REPORT_CRITIC_CAPPED = 8301  # 评审达到轮次上限（正常展示 + 未通过标记 + 遗留问题）
    REPORT_CRITIC_FAILED = 8401  # 评审环节本身异常（标记"未评审"，与"未通过"严格区分）
    REPORT_NOT_FOUND = 8004  # 报告不存在

    INTERNAL_ERROR = 5001

    INVALID_REQUEST = 1004  # 请求参数不合法（评测集缺字段等）

    RATE_LIMITED = 6101  # 触发租户配额：黄色横幅 + Retry-After 倒计时
    QUOTA_EXCEEDED = 6201  # 平台配额不足：运维事件


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


def warn_envelope(data: Any, message: str, trace_id: str, elapsed_ms: int) -> Envelope:
    """warn = 部分成功 + 部分告警，前端正常渲染并附加提示。"""
    return Envelope(
        status="warn",
        data=data,
        error=ErrorBody(code=0, message=message),
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
