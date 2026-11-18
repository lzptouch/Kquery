/** 聊天消息的**纯函数**操作（`X-22` 残余）。
 *
 * 为什么单独抽出来：`retry` 与「恢复历史会话」都是**纯数据变换**，
 * 但此前写在组件里，只能靠渲染整页 + mock 一堆接口才能验证——
 * 于是两条行为（重试会重复一组问答 / 历史轮次丢 trace 入口）在真实使用中
 * 走了好几个版本都没被发现。抽成纯函数后，边界（找不到提问、缺 trace_id）
 * 都能被直接断言。
 */
import type { ChatMessage, SessionDetail } from "../../types";

/** 从失败消息出发，找到它对应的那条用户提问并连同失败气泡一起摘掉。
 *
 * 返回 `null` 表示这条消息不是「一轮失败」的形状（找不到前置提问），
 * 调用方应当什么都不做——**不要**退化成"只重发不删除"，那正是重复问答的来源。
 */
export function dropFailedTurn(
  messages: ChatMessage[],
  failedId: string,
): { messages: ChatMessage[]; question: string } | null {
  const failedIdx = messages.findIndex((m) => m.id === failedId);
  if (failedIdx < 0) return null;

  let questionIdx = -1;
  for (let i = failedIdx - 1; i >= 0; i -= 1) {
    if (messages[i].role === "user") {
      questionIdx = i;
      break;
    }
  }
  if (questionIdx < 0) return null; // 首条就是助手消息：没有可重发的提问

  const questionId = messages[questionIdx].id;
  return {
    messages: messages.filter((m) => m.id !== failedId && m.id !== questionId),
    question: messages[questionIdx].content,
  };
}

/** 会话存档 → 可渲染的消息列表。
 *
 * `traceId` 必须跟着恢复（历史轮次的成本查看 / 调用链复制都靠它）；
 * 证据暂不恢复——按轮持久化证据的体积代价未决，`evidence` 保持空数组，
 * 引用角标在有证据时才是可点状态（见 `MessageBubble`）。
 */
export function turnsToMessages(
  turns: SessionDetail["turns"],
  makeId: () => string,
): ChatMessage[] {
  const restored: ChatMessage[] = [];
  for (const turn of turns) {
    restored.push({ id: makeId(), role: "user", content: turn.question, evidence: [] });
    restored.push({
      id: makeId(),
      role: "assistant",
      content: turn.answer,
      evidence: [],
      verdict: turn.verdict as ChatMessage["verdict"],
      answerSource: turn.answer_source as ChatMessage["answerSource"],
      traceId: turn.trace_id || undefined,
    });
  }
  return restored;
}
