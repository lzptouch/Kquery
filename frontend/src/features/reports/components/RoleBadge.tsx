/** Agent 角色标签（阶段 8 前端规范 §2.1 / §8.1）。
 * 角色色板：Planner 紫 / Researcher 蓝 / Validator 青 / Writer 琥珀 / Critic 红。
 * 必须带可读的职责说明（仅靠颜色图标不足以表达角色差异）。
 */
const ROLE_META: Record<string, { color: string; icon: string; duty: string }> = {
  planner: { color: "#8B5CF6", icon: "👑", duty: "拆解目标为子任务 DAG" },
  researcher: { color: "#3B82F6", icon: "🎓", duty: "执行子任务检索证据" },
  validator: { color: "#06B6D4", icon: "✓", duty: "评分并淘汰低质证据" },
  writer: { color: "#F59E0B", icon: "📝", duty: "整合证据撰写报告" },
  critic: { color: "#EF4444", icon: "🔍", duty: "多维评审并给出改进意见" },
};

export function RoleBadge({ role, count }: { role: string; count?: number }) {
  const meta = ROLE_META[role] ?? { color: "#94A3B8", icon: "•", duty: role };
  return (
    <span
      className="role-badge"
      style={{ background: `color-mix(in srgb, ${meta.color} 10%, transparent)`, color: meta.color }}
      title={`${role}：${meta.duty}`}
      aria-label={`角色 ${role}，职责：${meta.duty}`}
    >
      {meta.icon} {role}
      {count != null && count > 1 && <span className="mono"> ×{count}</span>}
    </span>
  );
}
