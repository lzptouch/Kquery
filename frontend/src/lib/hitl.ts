/**
 * 人在环（HITL）前端协议。
 *
 * 后端在执行写类工具**之前**挂起（`interrupt()`），SSE 发出 `awaiting_human`，
 * 前端渲染审批卡片 → 用户点"允许/拒绝" → 调 `/approve`（预审批）或
 * `/resume`（逐次决策）→ 后端恢复执行。
 *
 * **为什么要单列一个文件**：审批涉及三个端点 + 一个 SSE 事件 + 一个状态机，
 * 散落在 api-client / sse-client / ChatPage 里会让人看不出"这条链路的全貌"。
 */

/** SSE 事件 `awaiting_human` 的载荷 */
export interface AwaitingHumanPayload {
  run_id: string;
  /** 挂起原因：`tool_approval_required`（写类工具待批）/ 其它 */
  reason: string;
  /** 待批的工具名（`tool_approval_required` 时有值） */
  tool?: string;
  /** 这一步要做什么（给用户看的说明） */
  description?: string;
  step_index?: number;
  /** 可选动作：允许 / 拒绝 */
  actions?: string[];
  /** HITL 补充线索（`hint` 模式） */
  value?: string;
}

/** 审批卡片的状态（前端本地状态机） */
export type ApprovalState = "idle" | "pending" | "approving" | "approved" | "rejected" | "error";

/** 是否是"写类工具待批"（其余挂起原因走补充线索流程） */
export function isToolApproval(p: AwaitingHumanPayload | null): boolean {
  return !!p && p.reason === "tool_approval_required";
}
