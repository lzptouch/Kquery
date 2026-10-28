/** 知识库管理页：授权弹窗的勾选 / payload / 二次确认 / 影响人数回显。 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { KbPage } from "../src/features/admin/security/KbPage";

const KB_PRIVATE = {
  kb_id: "kb-secret", name: "机密库", description: "", tenant_id: "default",
  is_public: false, created_by: "admin", created_at: "", updated_at: "",
  doc_count: 2, granted_count: 1,
};
const KB_PUBLIC = {
  kb_id: "default", name: "默认知识库", description: "系统默认", tenant_id: "default",
  is_public: true, created_by: "system", created_at: "", updated_at: "",
  doc_count: 10, granted_count: 0,
};
const USERS = [
  { user_id: "admin", tenant_id: "default", roles: ["admin"], groups: [], enabled: true },
  { user_id: "lisi", tenant_id: "default", roles: ["sales"], groups: [], enabled: true },
  { user_id: "zhangsan", tenant_id: "default", roles: ["hr"], groups: [], enabled: true },
];

const mockSetGrants = vi.fn();
const mockFetchGrants = vi.fn();
const mockFetchKbs = vi.fn();

vi.mock("../src/lib/api-client", async () => {
  const actual = await vi.importActual<typeof import("../src/lib/api-client")>("../src/lib/api-client");
  return {
    ...actual,
    fetchKnowledgeBases: () => mockFetchKbs(),
    fetchUsers: async () => ({ status: "ok", data: USERS, error: null, meta: { trace_id: "", elapsed_ms: 0 } }),
    fetchKbGrants: (kbId: string) => mockFetchGrants(kbId),
    setKbGrants: (kbId: string, userIds: string[]) => mockSetGrants(kbId, userIds),
  };
});

async function renderAndOpenGrant() {
  render(<KbPage />);
  await waitFor(() => expect(screen.getByText("机密库")).toBeInTheDocument());
  const row = screen.getByText("机密库").closest("tr")!;
  await userEvent.click(row.querySelectorAll("button")[0]); // 授权
  await waitFor(() => expect(screen.getByText("授权：机密库")).toBeInTheDocument());
}

describe("KbPage 授权弹窗", () => {
  beforeEach(() => {
    mockSetGrants.mockReset();
    mockFetchGrants.mockReset();
    mockFetchKbs.mockReset();
    mockFetchKbs.mockResolvedValue({ status: "ok", data: [KB_PRIVATE, KB_PUBLIC], error: null, meta: { trace_id: "", elapsed_ms: 0 } });
    mockFetchGrants.mockResolvedValue({
      status: "ok",
      data: [{ kb_id: "kb-secret", user_id: "lisi", granted_by: "admin", granted_at: "", user_enabled: true, user_roles: ["sales"] }],
      error: null, meta: { trace_id: "", elapsed_ms: 0 },
    });
    mockSetGrants.mockResolvedValue({
      status: "ok",
      data: { kb_id: "kb-secret", impact: { added_count: 1, removed_count: 0, added_users: ["zhangsan"], removed_users: [], total_after: 2 }, risk: "high" },
      error: null, meta: { trace_id: "", elapsed_ms: 0 },
    });
  });

  it("renders_granted_state", async () => {
    await renderAndOpenGrant();
    const lisiLabel = screen.getByText(/lisi（sales）/);
    expect(lisiLabel.closest("label")!.querySelector("input")!).toBeChecked();
    const zhangsanLabel = screen.getByText(/zhangsan（hr）/);
    expect(zhangsanLabel.closest("label")!.querySelector("input")!).not.toBeChecked();
  });

  it("save_sends_full_user_ids_payload", async () => {
    await renderAndOpenGrant();
    await userEvent.click(screen.getByText(/zhangsan（hr）/));
    // 扩权需二次确认：输入库名解禁
    const confirm = screen.getByRole("dialog").querySelector(".probe-input") as HTMLInputElement;
    await userEvent.type(confirm, "机密库");
    await userEvent.click(screen.getByText("保存（显示影响人数）"));
    await waitFor(() => expect(mockSetGrants).toHaveBeenCalledWith("kb-secret", expect.arrayContaining(["lisi", "zhangsan"])));
  });

  it("expansion_requires_confirm_typing_kb_name", async () => {
    await renderAndOpenGrant();
    await userEvent.click(screen.getByText(/zhangsan（hr）/));
    const save = screen.getByText("保存（显示影响人数）");
    expect(save).toBeDisabled();
    await userEvent.type(screen.getByRole("dialog").querySelector(".probe-input") as HTMLInputElement, "机密库");
    expect(save).toBeEnabled();
  });

  it("shows_impact_after_save", async () => {
    await renderAndOpenGrant();
    await userEvent.click(screen.getByText(/zhangsan（hr）/));
    await userEvent.type(screen.getByRole("dialog").querySelector(".probe-input") as HTMLInputElement, "机密库");
    await userEvent.click(screen.getByText("保存（显示影响人数）"));
    await waitFor(() => expect(screen.getByText(/新增 1 人可访问，减少 0 人/)).toBeInTheDocument());
    expect(screen.getByText("高风险")).toBeInTheDocument();
  });

  it("list_load_failure_shows_error_and_retry", async () => {
    // T-W5：列表加载失败 → 错误提示 + 重试按钮（重试时恢复）
    mockFetchKbs.mockResolvedValueOnce({ status: "error", data: null, error: { code: 5001, message: "服务暂时不可用" }, meta: { trace_id: "", elapsed_ms: 0 } });
    render(<KbPage />);
    await waitFor(() => expect(screen.getByText("服务暂时不可用")).toBeInTheDocument());
    expect(screen.getByText("重试")).toBeInTheDocument();
    await userEvent.click(screen.getByText("重试"));
    await waitFor(() => expect(screen.getByText("机密库")).toBeInTheDocument());
  });
});
