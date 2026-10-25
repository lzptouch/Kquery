/** F2 语音按钮单测。 */
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { VoiceRecorder } from "../src/features/chat/components/VoiceRecorder";

const mockFetch = vi.fn();

function stubFetch(ok: boolean, data: unknown = null) {
  mockFetch.mockReset();
  mockFetch.mockResolvedValue({
    ok,
    status: ok ? 200 : 503,
    json: async () =>
      ok
        ? { status: "ok", data, error: null, meta: {} }
        : { status: "error", error: { code: 3803, message: "语音服务未配置" } },
  });
  global.fetch = mockFetch as unknown as typeof fetch;
}

describe("VoiceRecorder", () => {
  it("disabled_without_media_devices", async () => {
    // jsdom 没有 mediaDevices → 挂载即禁用
    stubFetch(true, { available: true, mode: "transcribe" });
    render(<VoiceRecorder onTranscript={() => {}} />);
    const button = screen.getByRole("button", { name: /语音输入/ });
    await waitFor(() => expect(button).toBeDisabled());
    expect(button.getAttribute("title")).toContain("不支持");
  });

  it("disabled_when_backend_unavailable", async () => {
    // 伪造 mediaDevices（浏览器支持），但后端说不可用
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
      getUserMedia: async () => ({ getTracks: () => [] }),
    };
    stubFetch(true, { available: false, mode: "none" });
    render(<VoiceRecorder onTranscript={() => {}} />);
    const button = screen.getByRole("button", { name: /语音输入/ });
    await waitFor(() => expect(button).toBeDisabled());
    expect(button.getAttribute("title")).toContain("语音服务未配置");
  });

  it("enabled_when_backend_available", async () => {
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
      getUserMedia: async () => ({ getTracks: () => [] }),
    };
    stubFetch(true, { available: true, mode: "transcribe" });
    render(<VoiceRecorder onTranscript={() => {}} />);
    const button = screen.getByRole("button", { name: /语音输入/ });
    await waitFor(() => expect(button).not.toBeDisabled());
  });

  it("uses_sts_mode_when_backend_says_so", async () => {
    (navigator as unknown as { mediaDevices: unknown }).mediaDevices = {
      getUserMedia: async () => ({ getTracks: () => [] }),
    };
    stubFetch(true, { available: true, mode: "sts" });
    render(<VoiceRecorder onTranscript={() => {}} />);
    const button = screen.getByRole("button", { name: /语音输入/ });
    await waitFor(() => expect(button).not.toBeDisabled());
    // STS 通路不该去请求转写端点
    const paths = mockFetch.mock.calls.map((c) => String(c[0]));
    expect(paths.some((p) => p.includes("/speech/transcribe"))).toBe(false);
  });
});
