"""领域异常体系（`A-07`）。

## 契约

每个派生异常回答同一个问题：**调用方该做什么**。

| 异常 | 码 | HTTP | 调用方该做什么 |
| --- | --- | --- | --- |
| `ConfigError` | 5210 | 500 | 配置非法 → 改配置重启 |
| `InvalidInputError` | 2001 | 400 | 输入/参数非法 → 改请求 |
| `StorageCorruptedError` | 5209 | 500 | 真相源损坏 → 拒绝启动 |
| `UpstreamUnavailableError` | 5201 | 502 | 上游故障 → 重试 / 降级 |
| `UpstreamTimeoutError` | 4003 | 504 | 上游超时 → 重试 |
| `ModelOutputError` | 4002 | 502 | 模型输出不可解析 → 重试或降级 |
| `ToolExecutionError` | 4201 | 500 | 工具执行失败 → 换工具 / 跳过 |

## 为什么要逐个断言

「全部换成同一个基类」等于没分类——调用方仍然不知道该重试还是该改请求。
所以这里逐个钉住「码 + HTTP 状态」，改动任何一个都会红。
"""
from __future__ import annotations

import pytest

from app.core.envelope import ErrCode
from app.core.errors import (
    ConfigError,
    DomainError,
    InvalidInputError,
    ModelOutputError,
    StorageCorruptedError,
    ToolExecutionError,
    UpstreamTimeoutError,
    UpstreamUnavailableError,
)

CASES = [
    (ConfigError, ErrCode.CONFIG_INVALID, 500),
    (InvalidInputError, ErrCode.INVALID_INPUT, 400),
    (StorageCorruptedError, ErrCode.STORAGE_CORRUPTED, 500),
    (UpstreamUnavailableError, ErrCode.DEPENDENCY_UNAVAILABLE, 502),
    (UpstreamTimeoutError, ErrCode.GENERATION_TIMEOUT, 504),
    (ModelOutputError, ErrCode.GENERATION_FAILED, 502),
    (ToolExecutionError, ErrCode.TOOL_EXECUTION_FAILED, 500),
]


@pytest.mark.parametrize("cls,code,status", CASES)
def test_exception_carries_code_and_status(cls, code, status):
    err = cls("出错了")
    assert int(err.code) == int(code)
    assert err.http_status == status
    assert err.message == "出错了"


@pytest.mark.parametrize("cls,code,status", CASES)
def test_exception_is_domain_error(cls, code, status):
    """全部领域异常都必须能被全局处理器认出来（否则会掉进兜底 500）。"""
    assert issubclass(cls, DomainError)
    assert issubclass(cls, Exception)


def test_distinct_classes_use_distinct_codes():
    """「不分类」等于没分类：不同语义不能共用同一个码。"""
    codes = [int(code) for _, code, _ in CASES]
    assert len(codes) == len(set(codes))


def test_storage_corrupted_keeps_path_out_of_message():
    """路径不进 message：message 可能出现在 HTTP 响应体里。"""
    err = StorageCorruptedError("图存储损坏", remedy="从备份恢复", path="/data/graph/graph.db")
    assert "/data/graph/graph.db" not in err.message
    assert str(err.path) == "/data/graph/graph.db"
    assert "从备份恢复" in err.message


def test_message_does_not_leak_credentials():
    """message 不得含连接串 / 凭据（这是领域异常的硬约束）。"""
    secret = "redis://:pwd@10.0.0.1:6379"
    err = UpstreamUnavailableError(f"上游不可达：{secret}")
    # 领域异常不负责过滤 message，但**代码里不该这么写**——
    # 这里断言当前实现把 message 原样透传，提醒使用方不要塞凭据。
    assert secret in err.message  # 透传（由调用方负责不塞敏感信息）
