"""结构化日志（TASK-133）。

此前是 `logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s %(message)s")`：
纯文本行，字段得靠正则从消息里抠。上了日志平台之后，"按 trace_id 串起一次请求"
"按 tenant 聚合错误率"这类查询都要写脆弱的正则。

本模块提供：
1. **JSON Formatter**：强制字段 `ts/level/logger/msg/trace_id/tenant_id/route/latency_ms`；
2. **PII 脱敏过滤器**：日志比答案更容易泄漏（堆栈里带参数、异常里带原文），
   所以脱敏要在**日志层**再做一次，而不是只依赖业务层的敏感词扫描；
3. **DEBUG 采样**：DEBUG 全量写会在高 QPS 下把磁盘打满，按比例采样；
   INFO 及以上**不采样**（线上排障靠它们）。

设计取舍：默认输出**保持纯文本**（`log_format="text"`），因为本地开发看 JSON 太痛苦；
生产显式设 `LOG_FORMAT=json`。
"""
import json
import logging
import re
import sys
from datetime import datetime, timezone

# 与 sensitive.py 的 PII 正则同源（那里用于答案掩码，这里用于日志脱敏）。
# 刻意不复用同一个对象：日志脱敏需要**更激进**（宁可多遮），
# 而答案掩码要考虑可读性，两者策略会分别演化。
_LOG_PII = [
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),          # 身份证
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),            # 手机号
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),        # 邮箱
    re.compile(r"(?<!\d)\d{16,19}(?!\d)"),              # 银行卡
    # 常见凭证形态：Bearer / api_key= / token=
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;\"'}]+"),
]

# 强制字段：日志平台据此建索引。缺失的补空串而不是省略——
# 字段时有时无会让下游的查询/看板时好时坏。
REQUIRED_FIELDS = ("ts", "level", "logger", "msg", "trace_id", "span_id",
                   "tenant_id", "user_id", "route", "latency_ms")


def redact(text: str) -> str:
    """日志脱敏：把 PII 与凭证形态替换为占位符。"""
    if not text:
        return text
    out = text
    for i, pat in enumerate(_LOG_PII[:4]):
        out = pat.sub("***", out)
    out = _LOG_PII[4].sub(r"\1***", out)
    out = _LOG_PII[5].sub(r"\1***", out)
    return out


class JsonFormatter(logging.Formatter):
    """JSON 行格式（每行一条，便于日志平台采集）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        # 从 LogRecord 上取调用方附加的上下文字段（由 tracing 中间件注入）
        for f in REQUIRED_FIELDS:
            if f in ("ts", "level", "logger", "msg"):
                continue
            payload.setdefault(f, getattr(record, f, "") or "")
        # 附加自定义字段（extra={...}）
        for key, value in getattr(record, "__dict__", {}).items():
            if key in payload or key.startswith("_") or key in (
                "args", "msg", "exc_info", "exc_text", "stack_info", "lineno",
                "funcName", "created", "msecs", "relativeCreated", "thread",
                "threadName", "processName", "process", "name", "levelname",
                "levelno", "pathname", "filename", "module", "taskName",
            ):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                payload[key] = redact(value) if isinstance(value, str) else value
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


class ContextFilter(logging.Filter):
    """把请求上下文（trace_id 等）注入每条日志记录。

    没有这一步，就必须在每个 `logger.info(...)` 手写 trace_id——现实中没人会坚持，
    结果就是"日志里能搜到一次请求，但拼不出一条链路"。
    trace_id 已由 `core/tracing.py` 的 contextvar 承载，这里只做搬运。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "trace_id", ""):
            try:
                from app.core.tracing import get_trace_id

                record.trace_id = get_trace_id() or ""
            except Exception:  # noqa: BLE001 - 日志不得因取 trace 失败而丢
                record.trace_id = ""
        return True


class SamplingFilter(logging.Filter):
    """DEBUG 采样（INFO 及以上全量）。

    用 `record.threadName`/计数做轮转式采样而不是 random：同一进程内行为可复现，
    测试不会因为随机性变 flaky。
    """

    def __init__(self, debug_rate: float = 0.1):
        super().__init__()
        self._rate = max(0.0, min(1.0, float(debug_rate)))
        self._counter = 0
        # 至少放行 1 条，否则 rate=0 时调试日志会完全消失（本地排障很痛苦）
        self._every = max(1, int(1 / self._rate)) if self._rate > 0 else 0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno > logging.DEBUG:
            return True
        if self._every == 0:
            return False
        self._counter += 1
        return self._counter % self._every == 1


def setup_logging(level: str = "INFO", log_format: str = "text", debug_sample_rate: float = 0.1) -> None:
    """配置根 logger。幂等：重复调用只更新 handler 配置。"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    # 上下文注入对所有形态生效（文本形态也带上 trace_id，排障时能直接 grep）
    handler.addFilter(ContextFilter())
    if str(log_format).lower() == "json":
        handler.setFormatter(JsonFormatter())
        # 采样只对 JSON（生产）形态生效；本地 text 形态保留全量 DEBUG
        handler.addFilter(SamplingFilter(debug_sample_rate))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)

    # 第三方库的 DEBUG 噪声在生产是纯负担
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio", "pymilvus"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
