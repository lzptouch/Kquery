/** F5 推荐追问按钮单测。 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MessageBubble } from "../../frontend/src/features/chat/components/MessageBubble";
import type { ChatMessage } from "../../frontend/src/types";

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "答案正文[#1]",
    evidence: [],
    ...overrides,
  };
}

describe("MessageBubble 推荐追问", () => {
  it("renders_followup_buttons", () => {
    render(
      <MessageBubble
        message={message({ followups: ["问题一？", "问题二？", "问题三？"] })}
        onCitationClick={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    const buttons = screen.getAllByRole("button", { name: /问题[一二三]？/ });
    expect(buttons).toHaveLength(3);
  });

  it("click_followup_triggers_callback", async () => {
    const onFollowup = vi.fn();
    render(
      <MessageBubble
        message={message({ followups: ["继续追问？"] })}
        onCitationClick={() => {}}
        onViewOriginal={() => {}}
        onFollowup={onFollowup}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "继续追问？" }));
    expect(onFollowup).toHaveBeenCalledWith("继续追问？");
  });

  it("no_buttons_without_followups", () => {
    const { container } = render(
      <MessageBubble message={message()} onCitationClick={() => {}} onViewOriginal={() => {}} />,
    );
    expect(container.querySelector(".followup-btn")).toBeNull();
  });

  it("no_buttons_while_streaming", () => {
    const { container } = render(
      <MessageBubble
        message={message({ streaming: true, followups: ["问题一？"] })}
        onCitationClick={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    expect(container.querySelector(".followup-btn")).toBeNull();
  });
});
