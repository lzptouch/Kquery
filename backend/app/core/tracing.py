"""全链路追踪号：每次问答 / 上传都有 trace_id，可关联检索与 LLM 调用（验收标准：日志可追溯）。

用 `contextvars` 承载而非逐层显式传参——否则任何新增的下游调用都必须记得"再传一遍
trace_id"，漏一处就断链，而漏掉的地方在代码里完全看不出来（实测 `get_trace_id()`
曾长期零调用）。

约定：
- 网关入口生成一次并 `set_trace_id`；
- 任何下游（异常处理器、审计、指标）用 `current_trace_id()` 读取，
  **异常路径必须复用同一个值**，否则用户从前端复制到的追踪号无法反查该请求日志。
"""
import contextvars
import time
import uuid

_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def set_trace_id(trace_id: str) -> str:
    """设置当前上下文的 trace_id，返回可回滚的 token。"""
    return _current_trace_id.set(trace_id)


def reset_trace_id(token) -> None:
    try:
        _current_trace_id.reset(token)
    except ValueError:  # token 属于别的上下文（线程切换），忽略即可
        pass


def get_trace_id() -> str:
    """读取当前 trace_id；未设置时生成一个并写入上下文（保证下游总能拿到非空值）。"""
    value = _current_trace_id.get()
    if not value:
        value = new_trace_id()
        _current_trace_id.set(value)
    return value


# 语义化别名：强调"读当前请求的追踪号"而不是"造一个新的"
current_trace_id = get_trace_id


def now_ms() -> float:
    return time.perf_counter() * 1000


def elapsed_ms_since(started_ms: float) -> int:
    return int(time.perf_counter() * 1000 - started_ms)