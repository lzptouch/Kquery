/** F10 快问会话维度：上传时带上 session_id。 */
import { render, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { QuickAttachPanel } from "../../frontend/src/features/chat/components/QuickAttachPanel";

const mockUpload = vi.fn();

vi.mock("../../frontend/src/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("../../frontend/src/lib/api-client")>(
    "../../frontend/src/lib/api-client",
  );
  return {
    ...actual,
    uploadQuickAsk: (file: File, sessionId?: string | null) => mockUpload(file, sessionId),
    deleteQuickAsk: async () => ({ status: "ok", data: { doc_id: "x", deleted: true } }),
  };
});

describe("QuickAttachPanel 会话绑定", () => {
  it("passes_session_id_on_upload", async () => {
    mockUpload.mockReset();
    mockUpload.mockResolvedValue({
      status: "ok",
      data: { doc_id: "quick_1", filename: "a.md", chunks: 2, ttl_s: 7200 },
      error: null,
    });
    const onAttach = vi.fn();
    render(
      <QuickAttachPanel
        current={null}
        onAttach={onAttach}
        onDetach={() => {}}
        onClose={() => {}}
        sessionId="sess-A"
      />,
    );
    const file = new File(["# hi"], "a.md", { type: "text/markdown" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, file);
    await waitFor(() => expect(onAttach).toHaveBeenCalled());
    expect(mockUpload).toHaveBeenCalledWith(file, "sess-A");
  });

  it("uploads_without_session_when_absent", async () => {
    mockUpload.mockReset();
    mockUpload.mockResolvedValue({
      status: "ok",
      data: { doc_id: "quick_2", filename: "b.md", chunks: 1, ttl_s: 7200 },
      error: null,
    });
    const onAttach = vi.fn();
    render(
      <QuickAttachPanel
        current={null}
        onAttach={onAttach}
        onDetach={() => {}}
        onClose={() => {}}
      />,
    );
    const file = new File(["# hi"], "b.md", { type: "text/markdown" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await userEvent.upload(input, file);
    await waitFor(() => expect(onAttach).toHaveBeenCalled());
    expect(mockUpload).toHaveBeenCalledWith(file, undefined);
  });

  it("accept_attribute_includes_pptx", () => {
    render(
      <QuickAttachPanel current={null} onAttach={() => {}} onDetach={() => {}} onClose={() => {}} />,
    );
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input.getAttribute("accept")).toContain(".pptx");
  });
});
