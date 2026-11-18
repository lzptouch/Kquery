"""SSE 事件回放缓冲（`Last-Event-ID` 断点续传）。

## 为什么需要它

前端断流（代理静默断流 / 手机切后台 / Wi-Fi 抖动）后，唯一能做的只有
「重发一遍问题」——这既让用户丢掉已经看到的半截答案，也**把同一问题算两次钱**
（`KQ-35` 已经为"重复提问"埋了在飞去重，但断线重发仍是一次全新的生成）。

SSE 规范本来就给了标准解法：服务端给每帧一个 `id:`，客户端重连时回传
`Last-Event-ID`，服务端从断点之后继续发。要让它成立，**生成必须与连接解耦**：
否则连接一断，生成器就被关闭，没有东西可回放。

所以本模块只做一件事——**按 run 保存已发出的事件帧**，读写都由端点层驱动：

- 写入：泵任务（`api/ask.py` 的 `_pump`）边生成边 `append`；
- 读取：连接侧的尾随循环 `frames_after(cursor)`，原连接与重连连接**读同一份缓冲**。

## 边界（都是刻意的）

1. **有界**：每个 run 最多 `max_frames` 帧，全局最多 `max_runs` 个 run，`ttl_s` 后回收。
   超限就**停止缓冲**（`overflowed`）——流照常发，只是"这一段不能再续传"，
   绝不允许一条长答案把进程内存吃干净。溢出时前端拿不到 `id:` 前缀吗？不，帧照旧带
   `id:`，只是缓冲没留底 → 重连会得到 `STREAM_NOT_RESUMABLE`，语义诚实。
2. **归属**：run 记下 `tenant_id` / `user_id`，续传必须同主体，否则等于凭猜到 trace_id
   就能读别人的答案。
3. **进程内**：多副本部署时续传要粘到同一副本（网关层面的事）。这里不外置到 Redis，
   因为「断线续传」的窗口是秒级，跨副本迁移的成本与收益不成比例。
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field

# 全局上限：64 个 run ≈ 64 个进行中的流（远超单副本并发问答数）
MAX_RUNS = 64
# 单 run 帧上限：一帧一个 token 批次 / 事件，2000 帧够覆盖 2 万字答案
MAX_FRAMES = 2000
# 缓冲存活时间：断线后用户还可能点「继续」，但不会等两分钟
DEFAULT_TTL_S = 120.0


@dataclass
class Frame:
    seq: int
    line: str


@dataclass
class ReplayRun:
    trace_id: str
    tenant_id: str
    user_id: str
    created_at: float
    updated_at: float
    frames: list[Frame] = field(default_factory=list)
    next_seq: int = 1
    done: bool = False
    overflowed: bool = False
    # 重连接上来的时刻（单调时钟）：断线看门狗据此决定"还要不要取消生成"
    resume_attached_at: float = 0.0

    def frames_after(self, seq: int) -> list[Frame]:
        if not self.frames or seq >= self.frames[-1].seq:
            return []
        # 帧按 seq 单调追加，可以直接二分；这里用线性过滤保持实现简单（帧数有界）
        return [f for f in self.frames if f.seq > seq]


class SseReplayStore:
    """按 run 保存 SSE 帧。线程安全（泵任务在事件循环线程，读侧也可能在别的循环）。"""

    def __init__(
        self,
        *,
        max_runs: int = MAX_RUNS,
        max_frames: int = MAX_FRAMES,
        ttl_s: float = DEFAULT_TTL_S,
        clock=time.monotonic,
    ) -> None:
        self._max_runs = max(1, int(max_runs))
        self._max_frames = max(1, int(max_frames))
        self._ttl_s = max(1.0, float(ttl_s))
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, ReplayRun] = {}
        # 计数：溢出与过期的总数（运维要能看出"续传到底有没有在用/为什么没用"）
        self._overflow_total = 0
        self._expired_total = 0

    # ---------- 写侧 ----------

    def open(self, trace_id: str, *, tenant_id: str = "", user_id: str = "") -> ReplayRun:
        now = self._clock()
        with self._lock:
            self._evict_locked(now)
            run = self._runs.get(trace_id)
            if run is None:
                run = ReplayRun(
                    trace_id=trace_id, tenant_id=tenant_id, user_id=user_id,
                    created_at=now, updated_at=now,
                )
                self._runs[trace_id] = run
            return run

    def append(self, trace_id: str, event: str, payload: dict) -> str:
        """记录一帧并返回可原样写出的 SSE 文本（带 `id:`）。

        run 不存在（缓冲已过期 / 未启用）时**仍然返回**一帧合法文本，只是不带 `id:`
        ——流不能因为"没留底"而断。
        """
        line_body = _frame_text(event, payload)
        with self._lock:
            run = self._runs.get(trace_id)
            if run is None:
                return line_body
            if run.overflowed or len(run.frames) >= self._max_frames:
                if not run.overflowed:
                    run.overflowed = True
                    self._overflow_total += 1
                run.updated_at = self._clock()
                return line_body
            seq = run.next_seq
            run.next_seq += 1
            run.updated_at = self._clock()
            line = f"id: {trace_id}:{seq}\n{line_body}"
            run.frames.append(Frame(seq=seq, line=line))
            return line

    def touch(self, trace_id: str) -> None:
        """心跳期间刷新存活时间（否则长答案静默期会被误判成过期）。"""
        with self._lock:
            run = self._runs.get(trace_id)
            if run is not None:
                run.updated_at = self._clock()

    def finish(self, trace_id: str) -> None:
        with self._lock:
            run = self._runs.get(trace_id)
            if run is not None:
                run.done = True
                run.updated_at = self._clock()

    def mark_resumed(self, trace_id: str) -> float:
        """记录"有人接上了"的时刻；返回该时刻（0 表示 run 已不存在）。"""
        with self._lock:
            run = self._runs.get(trace_id)
            if run is None:
                return 0.0
            run.resume_attached_at = self._clock()
            run.updated_at = run.resume_attached_at
            return run.resume_attached_at

    # ---------- 读侧 ----------

    def get(self, trace_id: str) -> ReplayRun | None:
        with self._lock:
            self._evict_locked(self._clock())
            return self._runs.get(trace_id)

    def frames_after(self, trace_id: str, seq: int) -> list[Frame]:
        run = self.get(trace_id)
        return run.frames_after(seq) if run is not None else []

    def is_done(self, trace_id: str) -> bool:
        run = self.get(trace_id)
        return run is None or run.done

    def is_resumable(self, trace_id: str, *, tenant_id: str, user_id: str) -> bool:
        """能否续传：存在、没溢出、归属一致。"""
        run = self.get(trace_id)
        if run is None or run.overflowed:
            return False
        if run.tenant_id and tenant_id != run.tenant_id:
            return False
        return not (run.user_id and user_id != run.user_id)

    def stats(self) -> dict:
        with self._lock:
            self._evict_locked(self._clock())
            return {
                "runs": len(self._runs),
                "frames": sum(len(r.frames) for r in self._runs.values()),
                "overflow_total": self._overflow_total,
                "expired_total": self._expired_total,
                "max_runs": self._max_runs,
                "max_frames": self._max_frames,
                "ttl_s": self._ttl_s,
            }

    # ---------- 内部 ----------

    def _evict_locked(self, now: float) -> None:
        stale = [k for k, r in self._runs.items() if now - r.updated_at > self._ttl_s]
        for k in stale:
            del self._runs[k]
            self._expired_total += 1
        # 仍然超出上限：先丢已结束的（最旧的先），再丢最旧的进行中
        if len(self._runs) > self._max_runs:
            finished = sorted(
                (r for r in self._runs.values() if r.done), key=lambda r: r.updated_at
            )
            while len(self._runs) > self._max_runs and finished:
                victim = finished.pop(0)
                self._runs.pop(victim.trace_id, None)
                self._expired_total += 1
            while len(self._runs) > self._max_runs:
                victim = min(self._runs.values(), key=lambda r: r.updated_at)
                self._runs.pop(victim.trace_id, None)
                self._expired_total += 1


def _frame_text(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def parse_last_event_id(raw: str | None) -> tuple[str, int]:
    """解析 `Last-Event-ID`：`{trace_id}:{seq}`（本服务格式）或裸 `seq`。

    返回 `(trace_id, seq)`；格式不认识时 `trace_id=""`、`seq=0`（＝从头开始），
    调用方负责用路径参数里的 trace_id 兜底。
    """
    if not raw:
        return "", 0
    text = raw.strip()
    if ":" in text:
        head, _, tail = text.rpartition(":")
        try:
            return head, int(tail)
        except ValueError:
            return "", 0
    try:
        return "", int(text)
    except ValueError:
        return "", 0
