"""`C-04`：取消必须在**阶段边界**生效，阻塞端点也必须能被打断（N-11/N-12）。

## 为什么这两件事要一起测

流式端点的取消早已接线（生成器关闭 → 置位事件 → 生成循环 break），但它只
覆盖「正在吐 token」这一段：预处理、检索、CRAG 精炼都在生成之前，一旦这些
阶段正在跑，服务端仍会算完。`_core_events` 的 docstring 声称「在阶段边界检查」，
代码里却只有生成循环——**文档比实现先走**是这类 bug 的典型伪装。

阻塞端点的问题更彻底：`/ask` 根本没有生成器可关闭，客户端走了服务端毫不知情，
一次「用户关页面」会继续烧完检索 + CRAG + 生成的全部上游成本，还占着在飞槽位
让下一次请求吃 503。

## 测试策略

- 用 `_NthCancel`（第 n 次 `is_set()` 起返回 True）把取消精确卡在某个边界上，
  比 `sleep` 之类的时间竞态稳定得多；
- 跑真实装配（`client` fixture）而不是假 self：这些检查点横跨预处理/检索/CRAG，
  假 self 会把「检查点被绕开」这件事一起假掉；
- 对 `refine` / `generate` 这类难以稳定触发的位置用源码断言兜底——它们防的是
  「有人顺手删掉一行检查」，行为测试已经覆盖了同名机制。
"""
from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from app.core.envelope import ErrCode
from app.core.identity import Principal
from app.services.generation import core as generation_core
from app.services.pipeline import PipelineCancelled, RagPipeline

_MD = "# 公司\n\nAcme 公司投资了 Beta 创业公司。Beta 的 CEO 是张三。\n"
_QUESTION = "Acme 投资了哪家公司？"


class _NthCancel:
    """第 `flip_at` 次 `is_set()` 起返回 True（前几次视为「还活着」）。

    接口与 `threading.Event` 对齐，管线不需要知道这是替身。
    """

    def __init__(self, flip_at: int) -> None:
        self.flip_at = flip_at
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        return self.checks >= self.flip_at

    def set(self) -> None:
        self.checks = 10 ** 6


def _scoped(client) -> RagPipeline:
    return client.app.state.pipeline.bind_principal(
        "default", "local", Principal(),
    )


@pytest.fixture()
def seeded(client):
    client.post(
        "/knowledge/upload",
        files={"file": ("公司.md", _MD.encode("utf-8"), "text/markdown")},
    )
    return client


class TestStageBoundaries:
    def test_disconnect_before_retrieval_aborts_at_preprocess(self, seeded):
        """预处理（意图/改写，最多 3 次模型调用）之后就断开 → 不该再检索。"""
        pipeline = _scoped(seeded)
        with pytest.raises(PipelineCancelled) as excinfo:
            pipeline.ask(_QUESTION, "default", "trace-pre", cancel=_NthCancel(1))
        assert excinfo.value.stage == "preprocess"

    def test_disconnect_during_retrieval_aborts_after_retrieve(self, seeded):
        """第 1 次检查放行、第 2 次（CRAG 轮次开头）放行、第 3 次（检索后）断开。"""
        pipeline = _scoped(seeded)
        with pytest.raises(PipelineCancelled) as excinfo:
            pipeline.ask(_QUESTION, "default", "trace-ret", cancel=_NthCancel(3))
        assert excinfo.value.stage == "retrieve"

    def test_all_boundaries_are_wired(self):
        """源码断言：preprocess / 每轮检索 / refine / generate 四处都要有检查。

        这几行没有返回值、没有副作用，删掉不会有任何测试变红——正是「顺手
        重构掉」的高危代码，所以必须显式钉住。
        """
        # `B0 X-27` 第二步：编排已搬到 `generation/core.py`，
        # 断言必须指向**真正执行的那份源码**，否则「钉住」变成「钉住一个转发函数」
        src = inspect.getsource(generation_core.core_events)
        assert '_abort_stage("preprocess")' in src
        assert '_abort_stage(f"crag_round_{round_no}")' in src
        assert '_abort_stage("retrieve")' in src
        assert '_abort_stage("refine")' in src
        assert '_abort_stage("generate")' in src

    def test_cancelled_answer_is_neither_cached_nor_recorded(self, seeded, monkeypatch):
        """取消不是失败：不进缓存（半截答案会被复用），也不记 success 指标。"""
        pipeline = _scoped(seeded)
        stores: list = []
        records: list = []
        monkeypatch.setattr(RagPipeline, "_cache_store", lambda self, *a, **k: stores.append(a))
        monkeypatch.setattr(RagPipeline, "_record", lambda self, *a, **k: records.append(a))

        with pytest.raises(PipelineCancelled):
            pipeline.ask(_QUESTION, "default", "trace-nocache", cancel=_NthCancel(1))

        assert stores == [], "取消的回答被写进了缓存（下一次会拿到半截答案）"
        assert records == [], "取消被记成了成功/失败统计"

    def test_stream_cancel_emits_no_error_or_done(self, seeded):
        """流式：断开后既不 yield error（没人收），也不 yield done（没收尾）。"""
        pipeline = _scoped(seeded)
        events = list(
            pipeline.ask_stream(_QUESTION, "default", "trace-stream", cancel=_NthCancel(1))
        )
        kinds = [kind for kind, _ in events]
        assert "error" not in kinds
        assert "done" not in kinds

    def test_no_cancel_keeps_normal_path(self, seeded):
        """回归护栏：不传 cancel 时行为不变（别把正常请求也拦了）。"""
        pipeline = _scoped(seeded)
        result = pipeline.ask(_QUESTION, "default", "trace-ok")
        assert result.trace_id == "trace-ok"


def _run_coro(coro):
    """在独立线程里跑协程。

    不能用 `asyncio.run`：`pytest-playwright` 会让整个 session 处在一个
    running event loop 里（`tests/` 全量跑时才会暴露），`asyncio.run` 会直接
    抛 `RuntimeError: asyncio.run() cannot be called from a running event loop`。
    新开线程 + 新 loop 与当前线程的 loop 状态完全隔离。
    """
    box: dict = {}

    def _worker() -> None:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(coro)
            box["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - 原样回传给主线程断言
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(10)
    if "error" in box:
        raise box["error"]


class TestWatchDisconnect:
    def test_sets_cancel_when_client_is_gone(self):
        from app.api.ask import _watch_disconnect

        class _Request:
            def __init__(self) -> None:
                self.calls = 0

            async def is_disconnected(self) -> bool:  # noqa: D401 - 替身
                self.calls += 1
                return self.calls >= 2

        cancel = threading.Event()
        _run_coro(_watch_disconnect(_Request(), cancel, interval_s=0.01))
        assert cancel.is_set(), "探测到断开却没有置位取消事件"

    def test_stops_polling_once_cancelled(self):
        from app.api.ask import _watch_disconnect

        class _Request:
            def __init__(self) -> None:
                self.calls = 0

            async def is_disconnected(self) -> bool:
                self.calls += 1
                return False

        request = _Request()
        cancel = threading.Event()
        cancel.set()  # 生成器先于探测发现了断开
        _run_coro(_watch_disconnect(request, cancel, interval_s=0.01))
        assert request.calls == 0, "已取消还在轮询（白占一个协程）"


class TestBlockingEndpointCancel:
    def test_cancel_maps_to_499_without_error_counting(self, client, monkeypatch):
        """`/ask` 收到 `PipelineCancelled` → 499 + 4303，且不计错误指标。

        499 是 nginx 约定的「客户端关闭请求」：让网关把它从 5xx 里摘出去。
        计入错误率会把「用户走了」算成服务故障，告警阈值随之失真。
        """
        def _cancelled(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003 - 替身
            raise PipelineCancelled("generate")

        errors: list = []
        monkeypatch.setattr(RagPipeline, "ask", _cancelled)
        # `A-12` 拆分后错误指标入口在 `app.bootstrap`（异常处理器所在模块），
        # 补丁必须打在**处理器实际查名字的那个模块**上，否则守卫会变成空转。
        monkeypatch.setattr("app.bootstrap._record_error", lambda request, code: errors.append(code))

        resp = client.post("/ask", json={"question": _QUESTION})

        assert resp.status_code == 499, resp.text
        assert resp.json()["error"]["code"] == int(ErrCode.REQUEST_CANCELLED)
        assert errors == [], "取消被计入了错误指标"

    def test_inflight_slot_released_after_cancel(self, client, monkeypatch):
        """取消后必须释放「在飞去重」占位，否则同一问题会被永久 409。"""
        def _cancelled(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003 - 替身
            raise PipelineCancelled("preprocess")

        monkeypatch.setattr(RagPipeline, "ask", _cancelled)

        first = client.post("/ask", json={"question": _QUESTION})
        second = client.post("/ask", json={"question": _QUESTION})

        assert first.status_code == 499
        assert second.status_code == 499, "在飞占位没释放：同一问题第二次请求被 409 挡住"
