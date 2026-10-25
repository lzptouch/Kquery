/** F1 Markdown 渲染单测（B1）。 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Markdown, renderMarkdown } from "../src/features/chat/components/Markdown";

describe("Markdown 渲染", () => {
  it("renders_heading_and_list", () => {
    const { container } = render(<Markdown value={"# 标题\n\n- 第一项\n- 第二项"} />);
    expect(container.querySelector("h1")?.textContent).toBe("标题");
    expect(container.querySelectorAll("li")).toHaveLength(2);
  });

  it("renders_table", () => {
    const html = renderMarkdown("| 名称 | 值 |\n| --- | --- |\n| a | 1 |");
    expect(html).toContain("<table>");
    expect(html).toContain("<th");
  });

  it("renders_citation_chip", () => {
    const { container } = render(<Markdown value={"结论[#2]"} />);
    const chip = container.querySelector(".citation-chip");
    expect(chip).not.toBeNull();
    expect(chip?.getAttribute("data-cite-index")).toBe("2");
    expect(chip?.textContent).toBe("[#2]");
  });

  it("strips_raw_html", () => {
    const { container } = render(<Markdown value={"<script>alert(1)</script>正文"} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).not.toContain("alert(1)");
  });

  it("strips_inline_event_handler", () => {
    const { container } = render(<Markdown value={'<img src=x onerror=alert(1)>'} />);
    expect(container.querySelector("img")).toBeNull();
  });

  it("click_chip_triggers_callback", async () => {
    const onCitationClick = vi.fn();
    const { container } = render(<Markdown value={"答案[#2]"} onCitationClick={onCitationClick} />);
    const chip = container.querySelector(".citation-chip") as HTMLElement;
    await userEvent.click(chip);
    expect(onCitationClick).toHaveBeenCalledWith(2);
  });

  it("code_fence_not_parsed_as_cite", () => {
    const { container } = render(<Markdown value={"```\n引用示例 [#1]\n```"} />);
    expect(container.querySelector(".citation-chip")).toBeNull();
    expect(container.querySelector("pre code")?.textContent).toContain("[#1]");
  });

  it("inline_code_not_parsed_as_cite", () => {
    const { container } = render(<Markdown value={"行内代码 `[#1]` 不算角标"} />);
    expect(container.querySelector(".citation-chip")).toBeNull();
  });

  it("enable_citations_false_keeps_plain_text", () => {
    const { container } = render(<Markdown value={"结论[#2]"} enableCitations={false} />);
    expect(container.querySelector(".citation-chip")).toBeNull();
    expect(container.textContent).toContain("[#2]");
  });

  it("renders_empty_content", () => {
    const { container } = render(<Markdown value="" />);
    expect(container.textContent).toBe("");
  });

  it("keeps_accessible_name_on_chip", () => {
    render(<Markdown value={"结论[#3]"} />);
    expect(screen.getByRole("button", { name: /引用 3/ })).toBeTruthy();
  });
});
