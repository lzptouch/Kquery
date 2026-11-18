/**
 * 409「正在处理中」语义（`MP-03`）。
 *
 * ## 为什么单测
 *
 * 409 与 429/503 **语义不同**：409 = 同一个问题正在处理中（在飞去重），
 * 重试不会更快，**等前一个请求出结果才是正解**。
 * 前端此前把它折成「请求失败（HTTP 409）」，用户看到的像是系统坏了。
 */
import { describe, expect, it, vi } from "vitest";
import { request } from "../src/lib/api-client";
import { inProgressMessage, isInProgressStatus } from "../src/lib/retry-after";

function jsonResponse(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

const EMPTY_META = { trace_id: "", elapsed_ms: 0 };

describe("409 正在处理中", () => {
  it("识别 409 与其它状态", () => {
    expect(isInProgressStatus(409)).toBe(true);
    expect(isInProgressStatus(429)).toBe(false);
    expect(isInProgressStatus(503)).toBe(false);
  });

  it("409 → 给出「正在处理中」文案与可重试秒数", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(409, { status: "error", data: null, error: null, meta: EMPTY_META }, { "Retry-After": "7" }),
    );
    const body = await request("/ask", { method: "POST" });
    expect(body.status).toBe("error");
    expect(body.error?.message).toBe(inProgressMessage());
    expect(body.error?.retry_after).toBe(7);
    fetchSpy.mockRestore();
  });

  it("409 → 非 JSON 响应体也给出可读文案（不崩）", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("<html>502</html>", { status: 409, headers: { "Content-Type": "text/html" } }),
    );
    const body = await request("/ask", { method: "POST" });
    expect(body.status).toBe("error");
    expect(body.error?.message).toBe(inProgressMessage());
    expect(body.error?.retry_after).toBeGreaterThan(0);
    fetchSpy.mockRestore();
  });

  it("后端给了 message 时优先用后端的", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        409,
        {
          status: "error",
          data: null,
          error: { code: 6102, message: "同一请求正在处理中，请稍候再试" },
          meta: EMPTY_META,
        },
        { "Retry-After": "3" },
      ),
    );
    const body = await request("/ask", { method: "POST" });
    expect(body.error?.message).toBe("同一请求正在处理中，请稍候再试");
    expect(body.error?.retry_after).toBe(3);
    fetchSpy.mockRestore();
  });

  it("429 / 503 仍按背压处理（不被 409 的分支吞掉）", async () => {
    for (const status of [429, 503]) {
      const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
        jsonResponse(status, { status: "error", data: null, error: null, meta: EMPTY_META }),
      );
      const body = await request("/ask", { method: "POST" });
      expect(body.error?.message).not.toBe(inProgressMessage());
      expect(body.error?.retry_after).toBeGreaterThan(0);
      fetchSpy.mockRestore();
    }
  });

  it("500 仍不带 retry_after（不把故障伪装成「稍后再试」）", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(500, { status: "error", data: null, error: { code: 5000, message: "boom" }, meta: EMPTY_META }),
    );
    const body = await request("/ask", { method: "POST" });
    expect(body.error?.retry_after).toBeUndefined();
    fetchSpy.mockRestore();
  });
});
