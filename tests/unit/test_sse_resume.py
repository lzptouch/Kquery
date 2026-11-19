"""SSE 断点续传的**缓冲语义**与**断线宽限期**。

## 为什么这两块必须单独测

续传的失败模式都很安静：帧 id 重号 → 客户端续传时"跳过一半答案"，
用户看到的是**看似完整、实则截断**的回答；缓冲不设上限 → 一条超长答案把
进程内存吃光；宽限期逻辑写反 → 要么断线立刻取消（续传永远拿不到后半段），
要么永不取消（客户端走了还在烧 token）。

这些都**不会让既有用例变红**，所以逐条钉死。
"""
import asyncio
import threading
import time

import pytest

from app.api.ask import _watch_disconnect
from app.services.sse_replay import SseReplayStore, parse_last_event_id


def _store(**kw) -> SseReplayStore:
    kw.setdefault("ttl_s", 60.0)
    return SseReplayStore(**kw)


def _run_async(coro):
    """在**独立线程**里跑协程（该线程没有 running loop）。

    不能直接用 `asyncio.run`，也不能 `new_event_loop().run_until_complete`：
    pytest-playwright 的 sync API 在整个 pytest session 期间持有当前线程的
    running loop，两者分别抛
    `asyncio.run() cannot be called from a running event loop` 与
    `Cannot run the event loop while another loop is running`
    —— 表现为单独跑本文件全绿、和 `tests/e2e/ui` 同跑就 4 条全红。
    协程与主线程无共享 loop 状态，丢到子线程跑最省事。
    """
    outcome: dict[str, object] = {}

    def _target() -> None:
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - 原样抛回主线程
            outcome["error"] = exc

    worker = threading.Thread(target=_target, name="sse-resume-test")
    worker.start()
    worker.join()
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome.get("value")


# ---------------------------------------------------------------- 缓冲语义


def test_frames_get_monotonic_ids_and_full_sse_format():
    store = _store()
    store.open("t1", tenant_id="default", user_id="local")
    first = store.append("t1", "token", {"content": "你"})
    second = store.append("t1", "token", {"content": "好"})

    assert first.startswith("id: t1:1\n")
    assert second.startswith("id: t1:2\n")
    # 帧必须是完整可解析的 SSE 块（末尾双换行），否则前端解析器会粘帧
    assert first.endswith("\n\n")
    assert "event: token\n" in first
    assert '"content": "你"' in first


def test_frames_after_returns_only_the_tail():
    store = _store()
    store.open("t1")
    for i in range(5):
        store.append("t1", "token", {"content": str(i)})

    tail = store.frames_after("t1", 2)
    assert [f.seq for f in tail] == [3, 4, 5]
    # 断点就是最后一帧 → 没有新内容（而不是重发全部）
    assert store.frames_after("t1", 5) == []


def test_append_without_open_still_emits_valid_frame():
    """缓冲没开 / 已过期：流不能断，只是这一帧不带 id（不可续传）。"""
    store = _store()
    line = store.append("ghost", "token", {"content": "x"})
    assert "id:" not in line
    assert line.startswith("event: token\n")


def test_overflow_stops_buffering_but_keeps_streaming():
    store = _store(max_frames=3)
    store.open("t1")
    lines = [store.append("t1", "token", {"content": str(i)}) for i in range(5)]

    # 前 3 帧留底（带 id），第 4、5 帧照旧是合法 SSE（流不断）但**不带 id**：
    # id 一旦继续前进，客户端重连就会从"没留底的位置"开始要 → 拿到缺一段的假完整答案
    assert all("id:" in line for line in lines[:3])
    assert all("id:" not in line for line in lines[3:])
    assert store.stats()["frames"] == 3
    assert store.stats()["overflow_total"] == 1
    # 溢出后不允许续传（否则用户会拿到"缺一段"的假完整答案）
    assert store.is_resumable("t1", tenant_id="", user_id="") is False


def test_ttl_expiry_drops_run():
    now = [100.0]
    store = _store(ttl_s=10.0, clock=lambda: now[0])
    store.open("t1")
    store.append("t1", "token", {"content": "x"})

    now[0] += 11.0
    assert store.get("t1") is None
    assert store.stats()["expired_total"] == 1


def test_touch_keeps_long_stream_alive():
    now = [100.0]
    store = _store(ttl_s=10.0, clock=lambda: now[0])
    store.open("t1")
    for _ in range(3):
        now[0] += 6.0
        store.touch("t1")  # 心跳续命
    now[0] += 6.0
    assert store.get("t1") is not None  # 没有心跳的话这里已经过期


def test_max_runs_evicts_finished_first():
    store = _store(max_runs=2)
    store.open("a")
    store.finish("a")
    store.open("b")
    store.open("c")  # 超上限：先丢已结束的 a，保留进行中的 b/c
    assert store.get("a") is None
    assert store.get("b") is not None and store.get("c") is not None


def test_ownership_must_match_to_resume():
    store = _store()
    store.open("t1", tenant_id="t-a", user_id="u-a")
    assert store.is_resumable("t1", tenant_id="t-a", user_id="u-a") is True
    assert store.is_resumable("t1", tenant_id="t-b", user_id="u-a") is False
    assert store.is_resumable("t1", tenant_id="t-a", user_id="u-b") is False


def test_mark_resumed_timestamp_is_observable():
    store = _store()
    store.open("t1")
    assert store.get("t1").resume_attached_at == 0.0
    assert store.mark_resumed("t1") > 0.0
    assert store.get("t1").resume_attached_at > 0.0
    # run 不存在时返回 0（调用方据此走"不可续传"分支）
    assert store.mark_resumed("ghost") == 0.0


# ---------------------------------------------------------------- Last-Event-ID 解析


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("t1:7", ("t1", 7)),
        ("tr-abc:12", ("tr-abc", 12)),
        ("7", ("", 7)),
        ("", ("", 0)),
        (None, ("", 0)),
        ("garbage", ("", 0)),
        ("t1:not-a-number", ("", 0)),
    ],
)
def test_parse_last_event_id(raw, expected):
    assert parse_last_event_id(raw) == expected


# ---------------------------------------------------------------- 断线宽限期


class _FakeRequest:
    """`is_disconnected` 前 `after_calls` 次返回 False，之后返回 True。"""

    def __init__(self, after_calls: int = 0) -> None:
        self.calls = 0
        self.after_calls = after_calls

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self.after_calls


def test_disconnect_without_grace_cancels_immediately():
    cancel = threading.Event()
    _run_async(_watch_disconnect(_FakeRequest(after_calls=0), cancel, interval_s=0.01))
    assert cancel.is_set()


def test_grace_lets_a_resume_keep_generation_alive():
    store = _store()
    store.open("t1")
    cancel = threading.Event()

    async def scenario():
        # 宽限期 1s，每 0.2s 检查一次；在被取消之前"接上"
        async def resume_soon():
            await asyncio.sleep(0.15)
            store.mark_resumed("t1")

        await asyncio.gather(
            _watch_disconnect(
                _FakeRequest(after_calls=0), cancel,
                interval_s=0.01, grace_s=1.0, replay=store, trace_id="t1",
            ),
            resume_soon(),
        )

    _run_async(scenario())
    assert not cancel.is_set()  # 有人接上 → 不取消


def test_grace_without_resume_cancels_after_window():
    store = _store()
    store.open("t1")
    cancel = threading.Event()
    started = time.monotonic()
    _run_async(
        _watch_disconnect(
            _FakeRequest(after_calls=0), cancel,
            interval_s=0.01, grace_s=0.3, replay=store, trace_id="t1",
        )
    )
    assert cancel.is_set()
    assert time.monotonic() - started >= 0.25  # 确实等过窗口，而不是立刻取消


def test_finished_run_needs_no_cancel():
    """run 已结束（缓冲里 done）：没有什么可取消的，看门狗直接退出。"""
    store = _store()
    store.open("t1")
    store.finish("t1")
    cancel = threading.Event()
    _run_async(
        _watch_disconnect(
            _FakeRequest(after_calls=0), cancel,
            interval_s=0.01, grace_s=5.0, replay=store, trace_id="t1",
        )
    )
    assert not cancel.is_set()
