"""日志纪律（`A-15` / `A-16` / `A-18`）。

| 条目 | 要求 |
|---|---|
| `A-15` | 全仓 `logging.getLogger` 只允许出现在统一入口内部；业务代码一律走 `get_logger()` |
| `A-16` | 启动期回显生效配置，且**脱敏**（key / token / secret / password + 连接串口令） |
| `A-18` | 日志 `extra` 的长字段截断到 512 字符，防止单条日志膨胀 |
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from app.core.config import Settings, _mask_config_value
from app.core.logging_setup import JsonFormatter, get_logger

BACKEND_APP = Path(__file__).resolve().parents[2] / "backend" / "app"
LOGGING_SETUP = "core/logging_setup.py"


# ---------------- A-15 统一日志入口 ----------------

def test_no_bare_get_logger_outside_entrypoint():
    """除统一入口外，业务代码不得直接调 `logging.getLogger`。"""
    offenders: list[str] = []
    for path in sorted(BACKEND_APP.rglob("*.py")):
        rel = str(path.relative_to(BACKEND_APP))
        if rel == LOGGING_SETUP:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "logging.getLogger" in line and line.strip().startswith(("logger", "logging", "return", "root")):
                offenders.append(f"{rel}:{i}")
    assert not offenders, f"绕过统一日志入口的 logger：{offenders}"


def test_entrypoint_keeps_at_most_two_direct_calls():
    """入口内部只允许两处直接调用：工厂本身 + 根 logger。"""
    text = (BACKEND_APP / LOGGING_SETUP).read_text(encoding="utf-8")
    calls = [
        line for line in text.splitlines()
        if "logging.getLogger" in line and not line.strip().startswith("#")
        and "不再直接调" not in line
    ]
    assert len(calls) <= 2, calls


def test_get_logger_returns_named_logger():
    logger = get_logger("app.demo")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "app.demo"
    assert get_logger().name == "app.core.logging_setup"


# ---------------- A-16 启动期脱敏配置回显 ----------------

def test_mask_by_field_name():
    for name in ("dashscope_api_key", "metrics_token", "web_search_api_key", "some_password", "SECRET"):
        assert _mask_config_value(name, "super-secret") == "***"


def test_mask_empty_secret_stays_empty():
    """没配的 key 显示空串而不是 `***`——`***` 会让人以为配过。"""
    assert _mask_config_value("dashscope_api_key", "") == ""


def test_mask_dsn_password():
    masked = _mask_config_value("redis_url", "redis://user:pwd@10.0.0.1:6379/0")
    assert "pwd" not in masked
    assert "10.0.0.1:6379" in masked


def test_effective_summary_masks_everything_sensitive():
    settings = Settings(_env_file=None)
    settings.dashscope_api_key = "sk-real-key"
    settings.metrics_token = "tok-123"
    summary = settings.effective_summary()

    assert summary["dashscope_api_key"] == "***"
    assert summary["metrics_token"] == "***"
    # 非敏感字段照常回显（这行日志的价值就在这里）
    assert summary["llm_model"]
    assert summary["vector_store"]
    assert "resolved_provider" in summary


def test_effective_summary_is_json_serializable():
    settings = Settings(_env_file=None)
    text = json.dumps(settings.effective_summary(), ensure_ascii=False)
    assert "llm_model" in text


# ---------------- A-18 extra 字段截断 ----------------

def _record(msg: str = "x", **extra) -> logging.LogRecord:
    record = logging.LogRecord("demo", logging.INFO, __file__, 1, msg, None, None)
    for k, v in extra.items():
        setattr(record, k, v)
    return record


def test_extra_field_truncated_to_512():
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_record("hi", big="y" * 5000)))
    assert len(payload["big"]) < 600
    assert payload["big"].endswith("字符]")


def test_short_extra_field_untouched():
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_record("hi", small="ok")))
    assert payload["small"] == "ok"


def test_long_message_truncated():
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_record("z" * 5000)))
    assert len(payload["msg"]) < 600


def test_truncation_marker_states_lost_length():
    formatter = JsonFormatter()
    payload = json.loads(formatter.format(_record("hi", big="y" * 5000)))
    # 标注丢了多少字符：否则读日志的人不知道「被截了」还是「本来就短」
    assert re.search(r"\+\d+字符", payload["big"])
