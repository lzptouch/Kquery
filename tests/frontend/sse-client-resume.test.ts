/** 流式断线续传（`Last-Event-ID`）。
 *
 * 三条纪律（写错任何一条都会让用户看到"假完整"的答案或重复付费）：
 *
 * 1. **只在服务端声明时重连**（`X-SSE-Resume: enabled`）。没有这个头就重连，
 *    等于重新发一次提问：新开一条流、重复计费、已看到的前半段被冲掉。
 * 2. 重连必须带 `Last-Event-ID`，且断点取自**最后一个真实的 id**，
 *    这样服务端只补断点之后的帧，不会把前半段再发一遍。
 * 3. 续不上（404 / 记录过期）要明确告诉用户"内容可能不完整"，
 *    而不是伪装成普通网络错误让人以为再点一次能接着看。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { askStream, type StreamHandlers } from "../../frontend/src/lib/sse-client";

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

/** 可控流：可以推数据、可以"断线"（error），也可以正常关闭。 */
function controlledStream(signal: AbortSignal | null | undefined, headers: Record<string, string> = {}) {
  const encoder = new TextEncoder();
  let ctrl!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(c) { ctrl = c; } });
  signal?.addEventListener("abort", () => {
    try {
      ctrl.error(new DOMException("Aborted", "AbortError"));
    } catch { /* 已关闭 */ }
  });
  return {
    response: new Response(stream, { status: 200, headers }),
    send: (text: string) => ctrl.enqueue(encoder.encode(text)),
    breakOff: () => ctrl.error(new Error("socket hang up")),
    close: () => ctrl.close(),
  };
}

const RESUME_HEADERS = { "X-SSE-Resume": "enabled" };
const TOKEN_FRAME = (seq: number, content: string) =>
  `id: tr-1:${seq}\nevent: token\ndata: {"content":"${content}"}\n\n`;
const DONE_FRAME = (seq: number) =>
  `id: tr-1:${seq}\nevent: done\ndata: {"trace_id":"tr-1","elapsed_ms":12}\n\n`;

describe("askStream 断线续传", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("连接中断后带 Last-Event-ID 续传，补上后半段", async () => {
    const onToken = vi.fn();
    const onDone = vi.fn();
    const onError = vi.fn();
    const controller = new AbortController();
    const fetchMock = vi.fn();

    const first = { stream: null as ReturnType<typeof controlledStream> | null };
    const second = { stream: null as ReturnType<typeof controlledStream> | null };
    fetchMock
      .mockImplementationOnce(async (_url: string, init: RequestInit) => {
        const s = controlledStream(init.signal, RESUME_HEADERS);
        first.stream = s;
        return s.response;
      })
      .mockImplementationOnce(async (_url: string, init: RequestInit) => {
        const s = controlledStream(init.signal, RESUME_HEADERS);
        second.stream = s;
        return s.response;
      });
    vi.stubGlobal("fetch", fetchMock);

    const pending = askStream("问题", controller.signal, makeHandlers({ onToken, onDone, onError }));
    await vi.advanceTimersByTimeAsync(10);
    first.stream!.send(TOKEN_FRAME(1, "前半"));
    await vi.advanceTimersByTimeAsync(10);
    first.stream!.breakOff(); // 代理把连接掐了：没收到 done
    await vi.advanceTimersByTimeAsync(500); // 过掉 300ms 退避

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [url, init] = fetchMock.mock.calls[1];
    expect(String(url)).toContain("/ask/stream/tr-1");
    expect((init as RequestInit).headers).toMatchObject({ "Last-Event-ID": "tr-1:1" });

    second.stream!.send(TOKEN_FRAME(2, "后半"));
    second.stream!.send(DONE_FRAME(3));
    second.stream!.close();
    await pending;

    expect(onToken.mock.calls.map((c) => c[0])).toEqual(["前半", "后半"]);
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled();
  });

  it("服务端没声明续传能力时不重连，按普通中断报错", async () => {
    const onError = vi.fn();
    const controller = new AbortController();
    const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
      const s = controlledStream(init.signal); // 没有 X-SSE-Resume
      const stream = s;
      queueMicrotask(() => {
        stream.send(TOKEN_FRAME(1, "a"));
        stream.breakOff();
      });
      return s.response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const pending = askStream("问题", controller.signal, makeHandlers({ onError }));
    await vi.advanceTimersByTimeAsync(2_000);
    await pending;

    expect(fetchMock).toHaveBeenCalledTimes(1); // 绝不自己重发问题
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toMatchObject({ code: 5001 });
  });

  it("续传拿不到记录时明确报 4304（内容可能不完整），不再重试", async () => {
    const onError = vi.fn();
    const controller = new AbortController();
    const fetchMock = vi.fn();
    const first = { stream: null as ReturnType<typeof controlledStream> | null };
    fetchMock
      .mockImplementationOnce(async (_url: string, init: RequestInit) => {
        const s = controlledStream(init.signal, RESUME_HEADERS);
        first.stream = s;
        return s.response;
      })
      .mockImplementation(async () => new Response("", { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);

    const pending = askStream("问题", controller.signal, makeHandlers({ onError }));
    await vi.advanceTimersByTimeAsync(10);
    first.stream!.send(TOKEN_FRAME(1, "a"));
    await vi.advanceTimersByTimeAsync(10);
    first.stream!.breakOff();
    await vi.advanceTimersByTimeAsync(2_000);
    await pending;

    expect(fetchMock).toHaveBeenCalledTimes(2); // 只试一次，不循环
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toMatchObject({ code: 4304 });
  });

  it("用户主动取消后不再续传", async () => {
    const onError = vi.fn();
    const controller = new AbortController();
    const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
      const s = controlledStream(init.signal, RESUME_HEADERS);
      queueMicrotask(() => s.send(TOKEN_FRAME(1, "a")));
      return s.response;
    });
    vi.stubGlobal("fetch", fetchMock);

    const pending = askStream("问题", controller.signal, makeHandlers({ onError }));
    await vi.advanceTimersByTimeAsync(50);
    controller.abort();
    await vi.advanceTimersByTimeAsync(2_000);
    await pending;

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(onError).not.toHaveBeenCalled();
  });
});
