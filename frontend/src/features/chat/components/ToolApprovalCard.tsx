import { useState } from "react";
import type { ApprovalState, AwaitingHumanPayload } from "../../../lib/hitl";

/**
 * 写类工具的**人工审批卡片**。
 *
 * 位置纪律：后端在执行写类工具**之前**挂起，所以这张卡片出现时，
 * 副作用（发请求 / 跑代码 / 调 MCP）**还没有发生**——
 * 用户点"允许"才会真的执行。
 *
 * 两个动作对应两个后端入口：
 * - 「允许并继续」= 逐次决策（`/resume` action=approve）：只放行这一次；
 * - 「本次运行都允许」= 预审批（`/approve`）：该 run 内后续同工具不再打断。
 */
interface Props {
  payload: AwaitingHumanPayload;
  /** 逐次决策：允许 / 拒绝 / 补充线索 */
  onDecide: (action: "approve" | "reject" | "hint", value: string) => Promise<void>;
  /** 预审批：对该 run 内的写类工具持续授权 */
  onApproveAll: (tool: string) => Promise<void>;
}

const TOOL_LABEL: Record<string, string> = {
  http_request: "发起 HTTP 请求",
  python_sandbox: "执行 Python 代码",
  mcp: "调用 MCP 工具",
};

export function ToolApprovalCard({ payload, onDecide, onApproveAll }: Props) {
  const [state, setState] = useState<ApprovalState>("pending");
  const [error, setError] = useState("");
  const [hint, setHint] = useState("");
  const tool = payload.tool ?? "";
  const label = TOOL_LABEL[tool] ?? tool;

  const run = async (fn: () => Promise<void>, next: ApprovalState) => {
    setState("approving");
    setError("");
    try {
      await fn();
      setState(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setState("error");
    }
  };

  if (state === "approved") {
    return (
      <div className="kq-approval" data-testid="tool-approval-approved">
        <span className="kq-approval__badge">已允许</span>
        <span className="kq-approval__body">
          {label} 已获授权，正在执行。
        </span>
      </div>
    );
  }
  if (state === "rejected") {
    return (
      <div className="kq-approval" data-testid="tool-approval-rejected">
        <span className="kq-approval__badge">已拒绝</span>
        <span className="kq-approval__body">已跳过 {label}，本步不再执行。</span>
      </div>
    );
  }

  return (
    <div className="kq-approval kq-approval--pending" data-testid="tool-approval-card">
      <div className="kq-approval__head">
        <span className="kq-approval__badge">需要人工批准</span>
        <span className="kq-approval__tool">{label}</span>
      </div>
      {payload.description ? (
        <p className="kq-approval__desc">{payload.description}</p>
      ) : null}
      <p className="kq-approval__hint">
        该工具会产生外部副作用（写操作 / 执行代码 / 调用外部服务）。
        在你允许之前，<strong>不会执行任何操作</strong>。
      </p>
      <div className="kq-approval__actions">
        <button
          type="button"
          className="kq-approval__btn kq-approval__btn--primary"
          disabled={state === "approving"}
          onClick={() => run(() => onDecide("approve", ""), "approved")}
        >
          允许并继续
        </button>
        <button
          type="button"
          className="kq-approval__btn"
          disabled={state === "approving" || !tool}
          onClick={() => run(() => onApproveAll(tool), "approved")}
        >
          本次运行都允许
        </button>
        <button
          type="button"
          className="kq-approval__btn kq-approval__btn--danger"
          disabled={state === "approving"}
          onClick={() => run(() => onDecide("reject", ""), "rejected")}
        >
          拒绝
        </button>
      </div>
      <details className="kq-approval__more">
        <summary>补充线索（给 Agent 一条提示）</summary>
        <textarea
          className="kq-approval__textarea"
          value={hint}
          placeholder="例如：只查 2024 年的数据"
          onChange={(e) => setHint(e.target.value)}
        />
        <button
          type="button"
          className="kq-approval__btn"
          disabled={state === "approving" || !hint.trim()}
          onClick={() => run(() => onDecide("hint", hint.trim()), "approved")}
        >
          提交线索并继续
        </button>
      </details>
      {error ? <p className="kq-approval__error">{error}</p> : null}
    </div>
  );
}

export default ToolApprovalCard;
