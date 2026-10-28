/** DocumentUploader：目标知识库下拉（多库时出现、选择随上传透传、单库时不显示）。 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentUploader } from "../src/features/knowledge/components/DocumentUploader";

const mockUpload = vi.fn();
const mockFetchKbs = vi.fn();

vi.mock("../src/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("../src/lib/api-client")>("../src/lib/api-client");
  return {
    ...actual,
    uploadFile: (file: File, onProgress: (p: number) => void, kbId?: string) => mockUpload(file, onProgress, kbId),
    fetchKnowledgeBases: () => mockFetchKbs(),
  };
});

const TWO_KBS = [
  { kb_id: "default", name: "默认知识库", is_public: true, doc_count: 0, granted_count: 0,
    description: "", tenant_id: "default", created_by: "system", created_at: "", updated_at: "" },
  { kb_id: "kb-secret", name: "机密库", is_public: false, doc_count: 2, granted_count: 1,
    description: "", tenant_id: "default", created_by: "admin", created_at: "", updated_at: "" },
];

function okKbList(data: unknown[]) {
  return { status: "ok", data, error: null, meta: { trace_id: "", elapsed_ms: 0 } };
}

async function pickFile() {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["# 内容"], "a.md", { type: "text/markdown" });
  await userEvent.upload(input, file);
}

describe("DocumentUploader 知识库下拉", () => {
  beforeEach(() => {
    mockUpload.mockReset();
    mockFetchKbs.mockReset();
    mockUpload.mockResolvedValue(okKbList([{ doc_id: "a", status: "indexed" }]));
  });

  it("shows_selector_and_passes_selected_kb", async () => {
    mockFetchKbs.mockResolvedValue(okKbList(TWO_KBS));
    render(<DocumentUploader onUploaded={() => {}} />);
    const select = await screen.findByLabelText("目标知识库");
    await userEvent.selectOptions(select, "kb-secret");
    await pickFile();
    await waitFor(() => expect(mockUpload).toHaveBeenCalled());
    expect(mockUpload.mock.calls[0][2]).toBe("kb-secret");
  });

  it("defaults_to_default_kb", async () => {
    mockFetchKbs.mockResolvedValue(okKbList(TWO_KBS));
    render(<DocumentUploader onUploaded={() => {}} />);
    await screen.findByLabelText("目标知识库");
    await pickFile();
    await waitFor(() => expect(mockUpload).toHaveBeenCalled());
    expect(mockUpload.mock.calls[0][2]).toBe("default");
  });

  it("selector_hidden_when_only_default_kb", async () => {
    mockFetchKbs.mockResolvedValue(okKbList([TWO_KBS[0]]));
    render(<DocumentUploader onUploaded={() => {}} />);
    await waitFor(() => expect(mockFetchKbs).toHaveBeenCalled());
    expect(screen.queryByLabelText("目标知识库")).not.toBeInTheDocument();
    await pickFile();
    await waitFor(() => expect(mockUpload).toHaveBeenCalled());
    expect(mockUpload.mock.calls[0][2]).toBe("default");
  });

  it("kb_list_failure_does_not_block_upload", async () => {
    mockFetchKbs.mockRejectedValue(new Error("网络异常"));
    render(<DocumentUploader onUploaded={() => {}} />);
    await pickFile();
    await waitFor(() => expect(mockUpload).toHaveBeenCalled());
  });
});
