import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { EvidenceCard } from "../src/features/chat/components/EvidenceCard";
import type { Evidence } from "../src/types";

/**
 * 图文回显（M6/M4）：文本证据关联了原图时，卡片要能显示图片。
 *
 * 旧实现只在 `modality === "image"` 时渲染图片——M4 绑定后**文字证据**也可能带图，
 * 旧条件会让这批图永远不显示（用户以为没有图）。
 */
const base: Evidence = {
  rank: 1,
  chunk_id: "doc#1",
  content: "这是流程图说明文字",
  source: "手册.pdf · 第2页",
  doc_id: "doc",
  filename: "手册.pdf",
};

describe("EvidenceCard 图文回显", () => {
  it("文字证据带 related_assets 时显示原图", () => {
    render(
      <EvidenceCard
        evidence={{ ...base, uri: "doc#page-2.png", related_assets: ["doc#page-2"] }}
        expanded
        highlighted={false}
        onToggle={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    const img = document.querySelector("img");
    expect(img).toBeTruthy();
    expect(img?.getAttribute("src")).toContain("page-2");
  });

  it("纯文本证据（有 uri 但无 related_assets）不显示图片", () => {
    render(
      <EvidenceCard
        evidence={{ ...base, uri: "doc#page-2.png" }}
        expanded
        highlighted={false}
        onToggle={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    expect(document.querySelector("img")).toBeNull();
  });

  it("图片证据（modality=image）照旧显示", () => {
    render(
      <EvidenceCard
        evidence={{ ...base, modality: "image", uri: "doc#page-2.png" }}
        expanded
        highlighted={false}
        onToggle={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    expect(document.querySelector("img")).toBeTruthy();
  });

  it("无图时只渲染正文", () => {
    render(
      <EvidenceCard
        evidence={base}
        expanded
        highlighted={false}
        onToggle={() => {}}
        onViewOriginal={() => {}}
      />,
    );
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByText("这是流程图说明文字")).toBeTruthy();
  });
});
