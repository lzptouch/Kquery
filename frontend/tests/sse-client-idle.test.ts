/** 流式问答的"连接已死"判定（`X-21` 残余）+ 多行 `data` 的端到端派发（`KQ-36`）。
 *
 * 背景：后端每 15s 发一条 `: ping` 注释行保活。如果前端既不解析注释行、
 * 也没有空闲判定，那么中间代理静默断流时 `reader.read()` **永不 settle**：
 * 页面停在 loading，用户拿不到答案也拿不到错误。这类故障在本地永远复现不了
 * （本地没有会静默丢包的代理），只能靠这里的可控流 + 假定时器钉住。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { askStream, type StreamHandlers } from "../src/lib/sse-client";

const IDLE_MS = 60_000;

function makeHandlers(overrides: Partial<StreamHandlers> = {}): StreamHandlers {
  const noop = () => {};
  return {
    onToken: noop,
    onEvidence: noop,
    onRetrievalStats: noop,
    onQueryAnalysis: noop,
    onCragBranch: noop,
    onRefine: noop,
    onConfidence: noop,
    onRefused: noop,
    onDone: noop,
    onError: noop,
    ...overrides,
  } as StreamHandlers;
}

/** 造一个"打开着但不再发数据"的流；signal abort 时让它以 AbortError 结束。 */
function hangingStream(signal: AbortSignal | null | undefined) {
  const encoder = new TextEncoder();
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      ctrl = c;
    },
  });
  signal?.addEventListener("abort", () => {
    try {
      ctrl.error(new DOMException("Aborted", "AbortError"));
    } catch {
      /* 流可能已关闭 */
    }
  });
  return {
    response: new Response(stream, { status: 200 }),
    send: (text: string) => ctrl.enqueue(encoder.encode(text)),
    close: () => ctrl.close(),
  };
}

describe("askStream 空闲判定", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("长时间无任何数据（含心跳）后以 9101 收尾，而不是永久停留", async () => {
    const onError = vi.fn();
    const controller = new AbortController();
    let target!: ReturnType<typeof hangingStream>;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      target = hangingStream(init.signal);
      return target.response;
    }));

    const pending = askStream("q", controller.signal, makeHandlers({ onError }));
    await vi.advanceTimersByTimeAsync(IDLE_MS + 1_000);
    await pending;

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toMatchObject({ code: 9101 });
  });

  it("心跳会重置空闲计时：持续保活就不算超时", async () => {
    const onError = vi.fn();
    const onDone = vi.fn();
    const onHeartbeat = vi.fn();
    const controller = new AbortController();
    let target!: ReturnType<typeof hangingStream>;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      target = hangingStream(init.signal);
      return target.response;
    }));

    const pending = askStream(
      "q",
      controller.signal,
      makeHandlers({ onError, onDone, onHeartbeat }),
    );
    // 两个 40s 的空档，各由一次心跳打断：累计 80s > 60s 阈值，但从未连续空闲 60s
    await vi.advanceTimersByTimeAsync(40_000);
    target.send(": ping\n\n");
    await vi.advanceTimersByTimeAsync(40_000);
    target.send('event: done\ndata: {"trace_id":"t","elapsed_ms":1}\n\n');
    target.close();
    await pending;

    expect(onHeartbeat).toHaveBeenCalled();
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled();
  });

  it("外部主动取消不报错（取消不是故障）", async () => {
    const onError = vi.fn();
    const controller = new AbortController();
    let target!: ReturnType<typeof hangingStream>;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      target = hangingStream(init.signal);
      return target.response;
    }));

    const pending = askStream("q", controller.signal, makeHandlers({ onError }));
    await vi.advanceTimersByTimeAsync(1_000);
    controller.abort();
    await vi.advanceTimersByTimeAsync(1_000);
    await pending;

    expect(onError).not.toHaveBeenCalled();
  });

  it("多行 data 拼接后仍能解析并派发", async () => {
    const onConfidence = vi.fn();
    const controller = new AbortController();
    let target!: ReturnType<typeof hangingStream>;
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init: RequestInit) => {
      target = hangingStream(init.signal);
      return target.response;
    }));

    const pending = askStream("q", controller.signal, makeHandlers({ onConfidence }));
    await vi.advanceTimersByTimeAsync(10);
    // 长 JSON 被折行：旧实现只保留最后一行 → JSON.parse 失败 → 静默丢弃
    target.send('event: confidence\ndata: {"verdict":"supported",\ndata: "score":12}\n\n');
    target.close();
    await pending;

    expect(onConfidence).toHaveBeenCalledWith(
      expect.objectContaining({ verdict: "supported", score: 12 }),
    );
  });
});
