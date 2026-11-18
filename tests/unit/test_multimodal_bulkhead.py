"""多模态并发闸门（`LM-03`）。

## 四条验收

1. 调用期间**持有许可**（占用 +1）
2. 许可耗尽 -> **快速失败**，不无限排队
3. 默认许可为 0 -> 直通且**行为与改造前一致**
4. 许可和 > `pipeline_workers` -> **启动校验报错**

## 为什么需要

视觉直答 / 图像 caption / 图像 embedding / OCR 此前**全部绕过隔离仓**，
只有指数退避重试：可以无限并发地压向上游平台。
后果是 `core/config.py` 里那条「许可之和 <= pipeline_workers」的硬约束被绕过——
线程全卡在等许可上，而「在飞请求数」看着很低。
"""
from __future__ import annotations

import threading
import time

import pytest

from app.services.upstream import Bulkhead, bulkhead, configure_bulkheads


@pytest.fixture()
def _reset_registry():
    import app.services.upstream as up

    with up._registry_lock:
        up._registry = {}
    yield
    with up._registry_lock:
        up._registry = {}


def test_multimodal_channel_is_registered(_reset_registry):
    """多模态通道进入隔离仓清单（`C-07` 已注册，`LM-03` 让它真正生效）。"""
    from app.core.config import Settings

    settings = Settings(_env_file=None)
    settings.upstream_multimodal_permits = 2
    configure_bulkheads(settings)
    stats = bulkhead("multimodal").stats()
    assert stats["max_permits"] == 2, stats
    assert stats["in_use"] == 0, stats


def test_zero_permits_passes_through(_reset_registry):
    """默认许可为 0 -> 直通（零行为变化）。"""
    from app.core.config import Settings

    settings = Settings(_env_file=None)
    settings.upstream_multimodal_permits = 0
    configure_bulkheads(settings)
    bh = bulkhead("multimodal")
    assert bh.stats()["max_permits"] == 0
    # 直通：拿许可不阻塞、也不拒绝
    with bh.acquire(timeout_s=0.1):
        pass


def test_permit_is_held_during_call(_reset_registry):
    """调用期间持有许可（占用 +1）。"""
    bh = Bulkhead("multimodal", permits=2)
    assert bh.stats()["in_use"] == 0
    with bh.acquire(timeout_s=0.5):
        assert bh.stats()["in_use"] == 1
    assert bh.stats()["in_use"] == 0


def test_exhausted_permits_fail_fast(_reset_registry):
    """许可耗尽 -> 快速失败（不无限排队）。"""
    bh = Bulkhead("multimodal", permits=1)
    started = time.monotonic()
    with bh.acquire(timeout_s=0.2):
        # 许可已被占满 -> 第二个申请必须**快速失败**
        with pytest.raises(Exception):
            bh.acquire(timeout_s=0.2)
    assert time.monotonic() - started < 2.0, "许可耗尽时不该长时间等待"


def test_waiting_for_permit_has_a_deadline(_reset_registry):
    """等待许可本身也有上限（否则请求会无限排队，而不是快速失败）。"""
    bh = Bulkhead("multimodal", permits=1)
    release = threading.Event()

    def _holder():
        with bh.acquire(timeout_s=1.0):
            release.wait(timeout=2.0)

    holder = threading.Thread(target=_holder)
    holder.start()
    time.sleep(0.05)
    started = time.monotonic()
    with pytest.raises(Exception):
        bh.acquire(timeout_s=0.2)
    waited = time.monotonic() - started
    release.set()
    holder.join(timeout=3)
    assert waited < 1.5, f"等待许可超时上限失效（等了 {waited:.2f}s）"


def test_startup_validates_permit_sum(_reset_registry):
    """许可和 > pipeline_workers -> 启动校验报错。"""
    from app.core.config import Settings

    settings = Settings(_env_file=None)
    settings.dashscope_api_key = "x"  # 避免其他必填项干扰
    settings.pipeline_workers = 4
    settings.upstream_llm_permits = 12
    settings.upstream_embedding_permits = 4
    settings.upstream_rerank_permits = 2
    settings.upstream_milvus_permits = 8
    settings.upstream_multimodal_permits = 2
    issues = settings.validate_startup()
    permit_issues = [i for i in issues if "permits" in i["field"]]
    assert permit_issues, "启动期未校验许可和（此前只有 scripts/capacity.py 会查）"
    assert "许可之和" in permit_issues[0]["reason"]


def test_default_permit_sum_is_within_workers():
    """默认配置下许可和 <= pipeline_workers（默认不该启动失败）。"""
    from app.core.config import Settings

    settings = Settings(_env_file=None)
    permit_sum = sum(
        int(getattr(settings, f"upstream_{n}_permits", 0) or 0)
        for n in ("llm", "embedding", "rerank", "milvus", "multimodal")
    )
    assert permit_sum <= settings.pipeline_workers, (
        f"默认许可和 {permit_sum} > pipeline_workers {settings.pipeline_workers}"
    )
