/** 聊天消息纯函数（`X-22` 残余）：
 * ① 重试必须把「提问 + 失败气泡」成对摘掉（否则重试一次多一组问答）；
 * ② 恢复历史会话必须带出该轮 `trace_id`（否则历史轮次的 trace 入口永久失效）。
 */
import { describe, expect, it } from "vitest";
import { dropFailedTurn, turnsToMessages } from "../../frontend/src/features/chat/message-ops";
import type { ChatMessage } from "../../frontend/src/types";

const user = (id: string, content: string): ChatMessage => ({
  id, role: "user", content, evidence: [],
});
const assistant = (id: string, content: string, extra: Partial<ChatMessage> = {}): ChatMessage => ({
  id, role: "assistant", content, evidence: [], ...extra,
});

describe("dropFailedTurn", () => {
  it("removes_question_and_failed_bubble_together", () => {
    const messages = [
      user("u1", "第一问"),
      assistant("a1", "第一答"),
      user("u2", "第二问"),
      assistant("a2", "", { error: { code: 9102, message: "网络错误" } }),
    ];
    const result = dropFailedTurn(messages, "a2");
    expect(result).not.toBeNull();
    expect(result!.question).toBe("第二问");
    expect(result!.messages.map((m) => m.id)).toEqual(["u1", "a1"]);
  });

  it("keeps_messages_after_the_failed_turn", () => {
    // 失败轮之后理论上不该有消息，但真出现（如断线后用户又发了一条）时不能连带删掉
    const messages = [
      user("u1", "问"),
      assistant("a1", "", { error: { code: 9101, message: "超时" } }),
      user("u2", "后续提问"),
      assistant("a2", "后续回答"),
    ];
    const result = dropFailedTurn(messages, "a1");
    expect(result!.messages.map((m) => m.id)).toEqual(["u2", "a2"]);
  });

  it("returns_null_when_no_preceding_question", () => {
    const messages = [assistant("a1", "", { error: { code: 9101, message: "超时" } })];
    expect(dropFailedTurn(messages, "a1")).toBeNull();
  });

  it("returns_null_for_unknown_id", () => {
    const messages = [user("u1", "问"), assistant("a1", "答")];
    expect(dropFailedTurn(messages, "nope")).toBeNull();
  });

  it("does_not_mutate_input", () => {
    const messages = [user("u1", "问"), assistant("a1", "", { error: { code: 9101, message: "x" } })];
    const before = messages.map((m) => m.id);
    dropFailedTurn(messages, "a1");
    expect(messages.map((m) => m.id)).toEqual(before);
  });
});

describe("turnsToMessages", () => {
  it("restores_pairs_in_order", () => {
    let seq = 0;
    const makeId = () => `m${++seq}`;
    const restored = turnsToMessages(
      [
        { question: "q1", answer: "a1", verdict: "answered", turn: 1, trace_id: "tr-1" },
        { question: "q2", answer: "a2", verdict: "answered", turn: 2, trace_id: "tr-2" },
      ],
      makeId,
    );
    expect(restored).toHaveLength(4);
    expect(restored.map((m) => m.role)).toEqual(["user", "assistant", "user", "assistant"]);
    expect(restored[1].traceId).toBe("tr-1");
    expect(restored[3].traceId).toBe("tr-2");
  });

  it("keeps_trace_id_undefined_when_absent", () => {
    // 旧会话文件里没有 trace_id：必须回落成 undefined，而不是空字符串
    //（空字符串会让「有 trace 入口」的判断为真，点开是 404）
    const restored = turnsToMessages(
      [{ question: "q", answer: "a", verdict: "answered", turn: 1 }],
      () => "x",
    );
    expect(restored[1].traceId).toBeUndefined();
  });

  it("keeps_verdict_and_answer_source", () => {
    const restored = turnsToMessages(
      [{
        question: "q", answer: "a", verdict: "refused", turn: 1,
        answer_source: "fallback" as never, trace_id: "t",
      }],
      () => "x",
    );
    expect(restored[1].verdict).toBe("refused");
    expect(restored[1].answerSource).toBe("fallback");
  });
});
