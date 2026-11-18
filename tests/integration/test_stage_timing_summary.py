"""阶段耗时汇总日志（`A-17`）。

**目标**：一次问答结束后打印一行

```
nodes={cache:2, retrieval:180, rerank:320, llm:1200} total=1853
```

慢在哪一段从此是**一行日志**的事，而不是靠日志时间戳人工对齐四段。
"""
from __future__ import annotations

import logging
import re

_NODES_RE = re.compile(r"nodes=\{(?P<stages>[^}]*)\} total=(?P<total>\d+)")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _capture_root() -> _Capture:
    handler = _Capture()
    logging.getLogger().addHandler(handler)
    return handler


def test_ask_logs_stage_summary(client):
    """问一次，断言有一行 `nodes={...} total=...`。"""
    handler = _capture_root()
    try:
        resp = client.post("/ask", json={"question": "阶段耗时汇总测试"})
        assert resp.status_code == 200, resp.text
    finally:
        logging.getLogger().removeHandler(handler)

    matches = [m for m in handler.messages if _NODES_RE.search(m)]
    assert matches, f"没有阶段耗时汇总日志：{handler.messages[-10:]}"

    msg = matches[-1]
    stages = dict(
        (k.strip(), int(v))
        for k, v in (part.split(":") for part in _NODES_RE.search(msg).group("stages").split(",") if part)
    )
    assert "cache" in stages, stages
    assert "retrieval" in stages, stages
    assert int(_NODES_RE.search(msg).group("total")) >= 0


def test_stage_summary_has_trace_id(client):
    """汇总行必须带 trace_id——否则没法和请求对上，等于白打。"""
    handler = _capture_root()
    try:
        client.post("/ask", json={"question": "阶段耗时 trace 关联"})
    finally:
        logging.getLogger().removeHandler(handler)

    matches = [m for m in handler.messages if _NODES_RE.search(m)]
    assert matches
    assert "trace_id=" in matches[-1]
