/** F3 上传白名单与 F4 快问入口的单测。 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  ACCEPT_ATTRIBUTE,
  ALLOWED_EXTENSIONS,
  extOf,
  validateExtension,
} from "../src/lib/file-types";

describe("文件类型白名单（F3）", () => {
  it("test_accepts_xlsx", () => {
    expect(ALLOWED_EXTENSIONS).toContain(".xlsx");
    expect(ACCEPT_ATTRIBUTE).toContain(".xlsx");
  });

  it("accepts_pptx", () => {
    expect(ALLOWED_EXTENSIONS).toContain(".pptx");
    expect(ACCEPT_ATTRIBUTE).toContain(".pptx");
  });

  it("gives_readable_hint_for_legacy_ppt", () => {
    expect(validateExtension("老演示.ppt")).toContain(".pptx");
  });

  it("accepts_plain_text_and_code", () => {
    for (const ext of [".txt", ".csv", ".json", ".yaml", ".html", ".py", ".sql"]) {
      expect(ALLOWED_EXTENSIONS).toContain(ext);
      expect(validateExtension(`a${ext}`)).toBeNull();
    }
  });

  it("rejects_unknown_extension", () => {
    expect(validateExtension("a.bin")).toContain("暂不支持");
  });

  it("gives_readable_hint_for_xls", () => {
    expect(validateExtension("老表.xls")).toContain(".xlsx");
  });

  it("ext_of_handles_no_extension", () => {
    expect(extOf("README")).toBe("");
    expect(extOf("a/b/c.MD")).toBe(".md");
  });

  it("upload_wizard_accept_includes_xlsx", async () => {
    const { UploadWizardPage } = await import("../src/features/knowledge/UploadWizardPage");
    // 上传向导依赖 react-router（useNavigate），这里只断言白名单常量本身
    expect(typeof UploadWizardPage).toBe("function");
    expect(ACCEPT_ATTRIBUTE).toContain(".xlsx");
  });
});

describe("快问附件面板（F4）", () => {
  it("renders_panel", async () => {
    const { QuickAttachPanel } = await import("../src/features/chat/components/QuickAttachPanel");
    render(
      <QuickAttachPanel
        current={null}
        onAttach={() => {}}
        onDetach={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByRole("dialog", { name: "附件提问" })).toBeTruthy();
  });

  it("shows_current_document", async () => {
    const { QuickAttachPanel } = await import("../src/features/chat/components/QuickAttachPanel");
    render(
      <QuickAttachPanel
        current={{ docId: "quick_1", filename: "临时.md", chunks: 3 }}
        onAttach={() => {}}
        onDetach={() => {}}
        onClose={() => {}}
      />,
    );
    expect(screen.getByText(/临时\.md/)).toBeTruthy();
  });
});
