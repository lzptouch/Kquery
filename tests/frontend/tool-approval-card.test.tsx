import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToolApprovalCard } from "../../frontend/src/features/chat/components/ToolApprovalCard";
import { isToolApproval } from "../../frontend/src/lib/hitl";

/**
 * 写类工具审批卡片的测试。
 *
 * 断言的是**行为**（点了会调什么、状态怎么变），
 * 而不是快照——快照会在任何样式改动时红，收益为零。
 */
describe("ToolApprovalCard", () => {
  const payload = {
    run_id: "t:u:s:1",
    reason: "tool_approval_required",
    tool: "http_request",
    description: "POST https://example.com/api",
    actions: ["approve", "reject"],
  };

  it("渲染待批工具与说明", () => {
    render(
      <ToolApprovalCard payload={payload} onDecide={async () => {}} onApproveAll={async () => {}} />,
    );
    expect(screen.getByTestId("tool-approval-card")).toBeTruthy();
    expect(screen.getByText("发起 HTTP 请求")).toBeTruthy();
    expect(screen.getByText("POST https://example.com/api")).toBeTruthy();
  });

  it("明确告知「允许前不会执行」", () => {
    render(
      <ToolApprovalCard payload={payload} onDecide={async () => {}} onApproveAll={async () => {}} />,
    );
    expect(screen.getByText(/不会执行任何操作/)).toBeTruthy();
  });

  it("点「允许并继续」→ 调用 approve 决策", async () => {
    const onDecide = vi.fn(async () => {});
    render(
      <ToolApprovalCard payload={payload} onDecide={onDecide} onApproveAll={async () => {}} />,
    );
    await userEvent.click(screen.getByText("允许并继续"));
    await waitFor(() => expect(onDecide).toHaveBeenCalledWith("approve", ""));
  });

  it("点「拒绝」→ 调用 reject 决策", async () => {
    const onDecide = vi.fn(async () => {});
    render(
      <ToolApprovalCard payload={payload} onDecide={onDecide} onApproveAll={async () => {}} />,
    );
    await userEvent.click(screen.getByText("拒绝"));
    await waitFor(() => expect(onDecide).toHaveBeenCalledWith("reject", ""));
  });

  it("点「本次运行都允许」→ 调用预审批", async () => {
    const onApproveAll = vi.fn(async () => {});
    render(
      <ToolApprovalCard payload={payload} onDecide={async () => {}} onApproveAll={onApproveAll} />,
    );
    await userEvent.click(screen.getByText("本次运行都允许"));
    await waitFor(() => expect(onApproveAll).toHaveBeenCalledWith("http_request"));
  });

  it("决策失败时显示错误而不是静默失败", async () => {
    const onDecide = vi.fn(async () => {
      throw new Error("网络错误");
    });
    render(
      <ToolApprovalCard payload={payload} onDecide={onDecide} onApproveAll={async () => {}} />,
    );
    await userEvent.click(screen.getByText("允许并继续"));
    await waitFor(() => expect(screen.getByText("网络错误")).toBeTruthy());
  });

  it("决策成功后展示已允许状态", async () => {
    const onDecide = vi.fn(async () => {});
    render(
      <ToolApprovalCard payload={payload} onDecide={onDecide} onApproveAll={async () => {}} />,
    );
    await userEvent.click(screen.getByText("允许并继续"));
    await waitFor(() => expect(screen.getByTestId("tool-approval-approved")).toBeTruthy());
  });
});

describe("isToolApproval", () => {
  it("只认 tool_approval_required", () => {
    expect(isToolApproval({ run_id: "r", reason: "tool_approval_required" })).toBe(true);
    expect(isToolApproval({ run_id: "r", reason: "need_hint" })).toBe(false);
    expect(isToolApproval(null)).toBe(false);
  });
});
