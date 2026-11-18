"""领域异常：携带错误码与 HTTP 状态码（`A-07` 的基类与派生类）。

## 为什么要有这层

裸异常（`raise RuntimeError` / `raise ValueError`）**不携带错误码**，因此：

1. 无法映射到统一信封，调用方只能靠猜；
2. 无法在指标里按类别聚合（"哪一类故障"是排障的第一问题）；
3. 调用方不知道上游抛的是什么，只能全接——这正是 61 处兜底 `except` 的成因。

## 字段语义

| 字段 | 含义 |
| --- | --- |
| `code` | 对应 `ErrCode` 的数值，默认内部错误 |
| `http_status` | 该语义对应的 HTTP 状态码，默认 500 |
| `message` | 面向人的可读描述（**不得**包含凭据 / 连接串 / 内部路径） |

## 派生异常按「调用方该怎么办」分类

不是按「哪个模块抛的」分类：

| 异常 | 码 | HTTP | 调用方该做什么 |
| --- | --- | --- | --- |
| `ConfigError` | 5210 | 500 | 配置非法 → 改配置重启 |
| `InvalidInputError` | 2001 | 400 | 输入/参数非法 → 改请求 |
| `StorageCorruptedError` | 5209 | 500 | 真相源损坏 → 拒绝启动（从备份恢复） |
| `UpstreamUnavailableError` | 5201 | 502 | 上游故障 → 可重试 / 降级 |
| `UpstreamTimeoutError` | 4003 | 504 | 上游超时 → 可重试 |
| `ModelOutputError` | 4002 | 502 | 模型输出不可解析 → 重试或降级 |
| `ToolExecutionError` | 4201 | 500 | 工具执行失败 → 换工具 / 跳过该步 |

## 全局处理器的位置纪律

领域异常处理器插在「API 错误处理器之后、兜底异常处理器之前」，
并保证**一次异常只在一处记堆栈**（处理函数记，端点不记）。
"""
from __future__ import annotations

from pathlib import Path

from app.core.envelope import ApiError, ErrCode


class DomainError(ApiError):
    """领域异常基类。

    复用 `ApiError` 的信封转换能力（全局处理器已经认它），
    区别只在于**语义命名**与「必须携带错误码」的约束。
    """


class ConfigError(DomainError):
    """配置非法：改配置重启（不是请求参数问题，所以是 500 而不是 400）。"""

    def __init__(self, message: str, *, http_status: int = 500) -> None:
        super().__init__(ErrCode.CONFIG_INVALID, message, http_status=http_status)


class InvalidInputError(DomainError):
    """输入/参数非法：调用方改请求即可（400）。"""

    def __init__(self, message: str, *, http_status: int = 400) -> None:
        super().__init__(ErrCode.INVALID_INPUT, message, http_status=http_status)


class StorageCorruptedError(DomainError):
    """真相源损坏：拒绝启动（`A-11`）。

    「真相源损坏即拒绝启动」是全局纪律：
    损坏的图谱若「从空图启动」，等于**静默丢数据**——用户以为图谱还在，
    实际已被清空，而且没有任何错误提示。

    ## 路径为什么不进 message

    `message` 可能出现在 HTTP 响应体里（请求期触发时），内部路径不该外泄。
    因此路径放在独立的 `path` 属性里，由启动日志打印。
    """

    def __init__(
        self,
        message: str,
        *,
        remedy: str = "",
        path: Path | str | None = None,
        http_status: int = 500,
    ) -> None:
        text = f"{message}；{remedy}" if remedy else message
        super().__init__(ErrCode.STORAGE_CORRUPTED, text, http_status=http_status)
        self.path = Path(path) if path is not None else None


class UpstreamUnavailableError(DomainError):
    """上游故障：可重试 / 降级（502）。

    `unsupported_endpoint=True` 表示**服务没有这个端点**（404 / 405）——
    这类故障**重试无意义**，必须给专门错误码（`EMBEDDING_ENDPOINT_UNSUPPORTED` /
    `RERANK_ENDPOINT_UNSUPPORTED`），否则调用方会以为是「服务暂时挂了」而一直重试。
    """

    def __init__(
        self,
        message: str,
        *,
        http_status: int = 502,
        unsupported_endpoint: bool = False,
        code: ErrCode | None = None,
    ) -> None:
        super().__init__(
            code or ErrCode.DEPENDENCY_UNAVAILABLE, message, http_status=http_status
        )
        self.unsupported_endpoint = unsupported_endpoint


class UpstreamTimeoutError(DomainError):
    """上游超时：可重试（504）。"""

    def __init__(self, message: str, *, http_status: int = 504) -> None:
        super().__init__(ErrCode.GENERATION_TIMEOUT, message, http_status=http_status)


class ModelOutputError(DomainError):
    """模型输出不可解析（缺 JSON / 结构不合法）：重试或降级（502）。"""

    def __init__(self, message: str, *, http_status: int = 502) -> None:
        super().__init__(ErrCode.GENERATION_FAILED, message, http_status=http_status)


class ToolExecutionError(DomainError):
    """工具执行失败（工具侧输入非法 / 工具崩了）：换工具或跳过该步（500）。"""

    def __init__(self, message: str, *, http_status: int = 500) -> None:
        super().__init__(ErrCode.TOOL_EXECUTION_FAILED, message, http_status=http_status)
