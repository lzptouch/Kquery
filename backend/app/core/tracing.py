"""全链路追踪号：每次问答 / 上传都有 trace_id，可关联检索与 LLM 调用（验收标准：日志可追溯）。"""
import contextvars
import time
import uuid

_current_trace_id: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def set_trace_id(trace_id: str) -> None:
    _current_trace_id.set(trace_id)


def get_trace_id() -> str:
    return _current_trace_id.get() or new_trace_id()


def now_ms() -> float:
    return time.perf_counter() * 1000


def elapsed_ms_since(started_ms: float) -> int:
    return int(time.perf_counter() * 1000 - started_ms)
