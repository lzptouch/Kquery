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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型检查期需要，避免为注解引入运行时依赖
    from threading import Thread

_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

#: 请求级上下文（X-15）：日志字段的**唯一生产者**。
#: 此前 tenant_id / user_id / route / latency_ms 四个字段"结构对了、内容空"——
#: 因为没有任何地方往日志记录里写它们。这里由中间件写进 contextvar，
#: 日志过滤器统一搬运，业务代码不必（也不应该）各自拼字段。
_request_context: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "request_context", default={}
)


def set_request_context(**fields) -> object:
    """写入请求上下文字段（返回可回滚的 token）。"""
    current = dict(_request_context.get() or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    return _request_context.set(current)


def get_request_context() -> dict:
    """读取当前请求上下文（副本，调用方改它不影响全局）。"""
    return dict(_request_context.get() or {})


def reset_request_context(token) -> None:
    try:
        _request_context.reset(token)
    except ValueError:
        pass


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


def spawn_thread(target, *, name: str, daemon: bool = True, args: tuple = (), kwargs: dict | None = None) -> "Thread":
    """创建线程并**继承当前上下文**（修 trace 断链）。

    `contextvars` 不随 `threading.Thread` 传播——这是 Python 的既定行为，不是 bug。
    后果是：图内所有日志的 trace_id 为空（`get_trace_id()` 会**新生成**一个），
    于是"一次请求的日志"在日志平台上被切成两段互不相关的记录，排障时根本串不起来。

     记录了这个缺陷。这里用 `contextvars.copy_context().run(...)` 把当前
    上下文快照带进新线程——注意必须 `copy_context()`（快照）而不是共享同一 context
    对象，否则两个线程会互相覆盖 trace_id。

    **所有后台线程都应通过本函数创建**，直接 `threading.Thread(...).start()`
    会让 trace 链再次断裂。
    """
    import contextvars as _cv
    import threading

    ctx = _cv.copy_context()
    call_kwargs = dict(kwargs or {})

    def _runner() -> None:
        # 在新线程里以主线程的上下文快照为起点执行
        ctx.run(target, *args, **call_kwargs)

    return threading.Thread(target=_runner, name=name, daemon=daemon)


def now_ms() -> float:
    return time.perf_counter() * 1000


def elapsed_ms_since(started_ms: float) -> int:
    return int(time.perf_counter() * 1000 - started_ms)
