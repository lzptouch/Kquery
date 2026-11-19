/**
 * 渲染期错误边界（`X-22` / `KQ-37`）。
 *
 * ## 钉住的是什么
 *
 * ① 子组件渲染期抛错时**不能整站白屏**：必须出现可操作的兜底（重试 / 重新加载）；
 * ② 兜底要给出定位线索（错误名 + 消息），否则线上只知道"崩了"；
 * ③ 「重试」必须真的重挂载子树——只清 `error` state 会让确定性异常立刻再抛，
 *    用户看到的是"点了没反应"。
 */
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "../../frontend/src/components/ErrorBoundary";

let shouldThrow = true;

function Flaky(): React.ReactElement {
  if (shouldThrow) throw new Error("渲染炸了：evidence.map is not a function");
  return <div>正常内容</div>;
}

describe("ErrorBoundary", () => {
  beforeEach(() => {
    shouldThrow = true;
    // React 会把捕获到的异常打一遍 console.error；用例里静音，避免污染输出
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("子组件抛错 → 渲染兜底而不是抛到顶层", () => {
    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("页面渲染出错");
    // 定位线索必须露出来：生产环境拿不到 trace_id，这是唯一的现场
    expect(alert).toHaveTextContent("渲染炸了");
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载页面" })).toBeInTheDocument();
  });

  it("点「重试」后子树重新挂载并恢复正常", () => {
    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();

    shouldThrow = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("正常内容")).toBeInTheDocument();
  });

  it("异常被记录到控制台（含组件栈），便于事后定位", () => {
    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    );
    expect(console.error).toHaveBeenCalled();
    const args = vi.mocked(console.error).mock.calls.flat().map(String);
    expect(args.join(" ")).toContain("ErrorBoundary");
  });

  it("没有异常时不额外包一层 DOM（不影响布局与样式选择器）", () => {
    shouldThrow = false;
    const { container } = render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    );
    expect(container.firstElementChild?.tagName).toBe("DIV");
    expect(container.firstElementChild?.textContent).toBe("正常内容");
  });
});
