/** F10 引文增强单测：参考汇总卡片 / 引文搜索 / 导出 TXT。 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MessageBubble } from "../src/features/chat/components/MessageBubble";
import { buildAnswerText, downloadAnswerAsTxt } from "../src/lib/export-txt";
import type { ChatMessage, Evidence } from "../src/types";

function evidence(overrides: Partial<Evidence> = {}): Evidence {
  return {
    rank: 1,
    chunk_id: "doc1#1",
    content: "Kquery 按问答次数计费",
    source: "产品手册 · 计费规则",
    doc_id: "doc1",
    filename: "产品手册.md",
    score: 0.9,
    ...overrides,
  };
}

function message(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "m1",
    role: "assistant",
    content: "按问答次数计费 [#1]",
    evidence: [],
    ...overrides,
  };
}

describe("参考汇总卡片", () => {
  it("shows_count_and_file_count", () => {
    render(
      <MessageBubble
        message={message({
          evidence: [
            evidence({ rank: 1 }),
            evidence({ rank: 2, doc_id: "doc2", filename: "报价.xlsx" }),
            evidence({ rank: 3, doc_id: "doc2", filename: "报价.xlsx" }),
          ],
        })}
        onCitationClick={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    const summary = screen.getByLabelText("参考");
    expect(summary.textContent).toContain("3 条引文");
    expect(summary.textContent).toContain("2 个文件");
  });

  it("hidden_without_evidence", () => {
    render(
      <MessageBubble message={message()} onCitationClick={() => {}} onViewOriginal={() => {}} />,
    );
    expect(screen.queryByLabelText("参考")).toBeNull();
  });
});

describe("引文搜索", () => {
  it("filters_evidence_by_keyword", async () => {
    render(
      <MessageBubble
        message={message({
          evidence: [
            evidence({ rank: 1, content: "计费按问答次数" }),
            evidence({ rank: 2, content: "支持 PDF 与 Markdown", chunk_id: "doc1#2" }),
          ],
        })}
        onCitationClick={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    // 过程区默认折叠 → 先展开
    await userEvent.click(screen.getByRole("button", { name: /检索过程与证据/ }));
    const input = screen.getByLabelText("在引文中搜索");
    await userEvent.type(input, "PDF");
    expect(screen.getByText(/匹配 1 \/ 2 条/)).toBeTruthy();
    expect(screen.queryByText("计费按问答次数")).toBeNull();
  });

  it("shows_empty_hint_when_no_match", async () => {
    render(
      <MessageBubble
        message={message({ evidence: [evidence()] })}
        onCitationClick={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: /检索过程与证据/ }));
    await userEvent.type(screen.getByLabelText("在引文中搜索"), "不存在的词");
    expect(screen.getByText("没有匹配的引文")).toBeTruthy();
  });
});

describe("导出 TXT", () => {
  it("builds_text_with_answer_and_evidence", () => {
    const text = buildAnswerText(
      message({ evidence: [evidence()], traceId: "trace-1" }),
      "计费规则是什么？",
    );
    expect(text).toContain("问题：计费规则是什么？");
    expect(text).toContain("按问答次数计费");
    expect(text).toContain("[#1] 产品手册.md");
    expect(text).toContain("Kquery 按问答次数计费");
    expect(text).toContain("trace-1");
  });

  it("triggers_download", () => {
    const createObjectURL = vi.fn(() => "blob:fake");
    const revokeObjectURL = vi.fn();
    (global as unknown as { URL: { createObjectURL: unknown; revokeObjectURL: unknown } }).URL = {
      createObjectURL,
      revokeObjectURL,
    } as never;
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    const name = downloadAnswerAsTxt(message({ content: "答案" }), "问题");
    expect(name).toMatch(/^kquery-answer-\d{8}\d{4}\.txt$/);
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    click.mockRestore();
  });

  it("button_present_in_message_actions", () => {
    render(
      <MessageBubble message={message()} onCitationClick={() => {}} onViewOriginal={() => {}} />,
    );
    expect(screen.getByLabelText("导出 TXT")).toBeTruthy();
  });
});
