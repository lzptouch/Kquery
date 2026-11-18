/**
 * 背压提示（`C-06`）：非流式请求遇到 429 / 503 时，
 * 信封必须带**可重试秒数**；真故障（500）**不得**带。
 *
 * ## 为什么断言「500 不带 retry_after」
 *
 * 把「服务挂了」伪装成「稍后再试」会让用户一直等一个不会自愈的失败——
 * 倒计时归零后再次失败，比直接报错更让人困惑。
 */
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { request } from "../src/lib/api-client";
import {
  DEFAULT_RETRY_AFTER_SECONDS,
  backpressureMessage,
  isBackpressureStatus,
  parseRetryAfter,
} from "../src/lib/retry-after";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

describe("retry-after helper", () => {
  it("解析整数秒数", () => {
    expect(parseRetryAfter(new Headers({ "Retry-After": "7" }))).toBe(7);
  });

  it("缺失 / 非法 → 保守缺省（不是 0，避免立刻重试打死马虎）", () => {
    expect(parseRetryAfter(new Headers())).toBe(DEFAULT_RETRY_AFTER_SECONDS);
    expect(parseRetryAfter(new Headers({ "Retry-After": "abc" }))).toBe(DEFAULT_RETRY_AFTER_SECONDS);
    expect(parseRetryAfter(new Headers({ "Retry-After": "0" }))).toBe(DEFAULT_RETRY_AFTER_SECONDS);
    expect(parseRetryAfter(new Headers({ "Retry-After": "-3" }))).toBe(DEFAULT_RETRY_AFTER_SECONDS);
  });

  it("上限 300 秒（不让前端干等一天）", () => {
    expect(parseRetryAfter(new Headers({ "Retry-After": "86400" }))).toBe(300);
  });

  it("只把 429 / 503 当作背压", () => {
    expect(isBackpressureStatus(429)).toBe(true);
    expect(isBackpressureStatus(503)).toBe(true);
    expect(isBackpressureStatus(500)).toBe(false);
    expect(isBackpressureStatus(502)).toBe(false);
  });

  it("文案区分限流与背压", () => {
    expect(backpressureMessage(429)).toContain("频繁");
    expect(backpressureMessage(503)).toContain("繁忙");
  });
});

describe("api-client 的背压处理", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("503 → 信封带 retry_after 与可读文案", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(503, { status: "error", data: null, error: { code: 6101, message: "服务繁忙，请稍后重试" }, meta: {} }, { "Retry-After": "9" }),
    );
    const env = await request("/admin/retrieval-config", { method: "PUT" });
    expect(env.status).toBe("error");
    expect(env.error?.retry_after).toBe(9);
    expect(env.error?.message).toContain("繁忙");
  });

  it("429 → 带 retry_after（限流也是「稍后再来」）", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(429, { status: "error", data: null, error: { code: 6101, message: "请求过于频繁" }, meta: {} }, { "Retry-After": "3" }),
    );
    const env = await request("/ask");
    expect(env.error?.retry_after).toBe(3);
  });

  it("500 → 不带 retry_after（不得把故障伪装成可重试）", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(500, { status: "error", data: null, error: { code: 5001, message: "服务暂时不可用" }, meta: {} }),
    );
    const env = await request("/ask");
    expect(env.status).toBe("error");
    expect(env.error?.retry_after).toBeUndefined();
  });

  it("非 JSON 的 503（网关 HTML）→ 仍带 retry_after，且不崩", async () => {
    fetchMock.mockResolvedValue(
      new Response("<html>502</html>", { status: 503, headers: { "Retry-After": "4" } }),
    );
    const env = await request("/knowledge/ingest", { method: "POST" });
    expect(env.status).toBe("error");
    expect(env.error?.retry_after).toBe(4);
    expect(env.error?.message).toContain("繁忙");
  });

  it("非 JSON 的 500 → 折成可读文案且不带 retry_after", async () => {
    fetchMock.mockResolvedValue(new Response("<html>500</html>", { status: 500 }));
    const env = await request("/knowledge/ingest", { method: "POST" });
    expect(env.status).toBe("error");
    expect(env.error?.message).toContain("HTTP 500");
    expect(env.error?.retry_after).toBeUndefined();
  });

  it("200 正常响应不被改写", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(200, { status: "ok", data: { ok: true }, error: null, meta: { trace_id: "t", elapsed_ms: 1 } }),
    );
    const env = await request<{ ok: boolean }>("/healthz");
    expect(env.status).toBe("ok");
    expect(env.data?.ok).toBe(true);
    expect(env.error).toBeNull();
  });
});
