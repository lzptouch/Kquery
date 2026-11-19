/**
 * 请求超时（`X-21` / `X-26`）。
 *
 * ## 为什么需要这些用例
 *
 * 超时逻辑最容易写出两种假绿：
 * ① 只断言「返回了错误信封」——但错误码是网络异常码，用户看到的是
 *    「无法连接到服务」，排障时被引去查网关，而真正原因是后端挂死；
 * ② 定时器没清干净——请求成功后 timer 还在跑，几十秒后把一个已完成的
 *    请求 abort 掉（表现为"偶发报错"，极难复现）。
 *
 * 所以这里逐条钉住：超时码 / 网络码 / 取消码三者不混、成功后不再触发 abort、
 * 以及「前端的超时窗口必须长于后端 deadline」这条纪律。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ASK_TIMEOUT_MS,
  CLIENT_CANCELLED_CODE,
  CLIENT_NETWORK_ERROR_CODE,
  CLIENT_TIMEOUT_CODE,
  DEFAULT_TIMEOUT_MS,
  UPLOAD_TIMEOUT_MS,
  ask,
  request,
} from "../../frontend/src/lib/api-client";

/** 模拟 fetch：订阅 signal，abort 时以 AbortError 拒绝（与真实实现一致）。 */
function hangingFetch(): ReturnType<typeof vi.fn> {
  return vi.fn(
    (_path: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () =>
          reject(new DOMException("Aborted", "AbortError")),
        );
      }),
  );
}

describe("api-client 请求超时", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("超时 → 9101 且文案说明是超时（不是网络异常）", async () => {
    fetchMock.mockImplementation(hangingFetch());
    const pending = request("/admin/metrics/summary", {}, { timeoutMs: 1000 });
    await vi.advanceTimersByTimeAsync(1000);
    const env = await pending;
    expect(env.status).toBe("error");
    expect(env.error?.code).toBe(CLIENT_TIMEOUT_CODE);
    expect(env.error?.message).toContain("超时");
    // 超时是「等太久」而不是「连不上」，不能带可重试秒数去引导用户立刻重试
    expect(env.error?.retry_after).toBeUndefined();
  });

  it("连不上（fetch 直接 reject）→ 9102 网络异常", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const env = await request("/healthz");
    expect(env.error?.code).toBe(CLIENT_NETWORK_ERROR_CODE);
    expect(env.error?.message).toContain("网络异常");
  });

  it("调用方主动取消 → 9103，不得报成超时", async () => {
    fetchMock.mockImplementation(hangingFetch());
    const controller = new AbortController();
    const pending = request("/ask", { signal: controller.signal }, { timeoutMs: 60_000 });
    controller.abort();
    const env = await pending;
    expect(env.error?.code).toBe(CLIENT_CANCELLED_CODE);
  });

  it("请求成功后定时器必须清掉（否则几十秒后 abort 一个已完成请求）", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ status: "ok", data: { ok: true }, error: null, meta: {} }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const env = await request<{ ok: boolean }>("/healthz", {}, { timeoutMs: 1000 });
    expect(env.status).toBe("ok");
    // 把时间推过超时点：若 timer 没清，这里会再走一次 abort 分支
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("timeoutMs=0 → 不设超时（长轮询场景自己管取消）", async () => {
    fetchMock.mockImplementation(hangingFetch());
    const pending = request("/ask/runs/x/interrupt", {}, { timeoutMs: 0 });
    await vi.advanceTimersByTimeAsync(10 * 60 * 1000);
    // 仍然挂着：没有任何 abort 发生
    expect(vi.getTimerCount()).toBe(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    pending.catch(() => {});
  });
});

describe("超时窗口与后端 deadline 的关系", () => {
  it("问答超时必须长于后端 ask_deadline_ms（120s），否则前端先断、答案白算", () => {
    expect(ASK_TIMEOUT_MS).toBeGreaterThan(120_000);
  });

  it("上传窗口给足（大文件），且默认窗口只适合只读接口", () => {
    expect(UPLOAD_TIMEOUT_MS).toBeGreaterThanOrEqual(120_000);
    expect(DEFAULT_TIMEOUT_MS).toBeLessThan(ASK_TIMEOUT_MS);
  });
});

describe("按语义选超时窗口", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("ask() 用的是长窗口：90s 时还在等，130s 才超时", async () => {
    fetchMock.mockImplementation(hangingFetch());
    const pending = ask("测试问题");
    await vi.advanceTimersByTimeAsync(90_000);
    // 90s 还没超时（若错用默认 30s，这里已经拿到 9101 了）
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(ASK_TIMEOUT_MS - 90_000);
    const env = await pending;
    expect(env.error?.code).toBe(CLIENT_TIMEOUT_CODE);
  });
});
