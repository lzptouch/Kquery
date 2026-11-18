/**
 * 检索配置页的「实验分组」视图（`MP-02`）。
 *
 * ## 为什么单测
 *
 * 后端早就有了灰度覆盖（`/admin/retrieval-config/scoped`：作用域 + 稳定哈希分桶），
 * 但页面上看不到「谁在被实验覆盖、覆盖了多少」——灰度就成了盲开盲关。
 * 视图最关键的是**口径说清楚**：定向 vs 分桶、优先级、生效状态。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { RetrievalConfigPage } from "../src/features/admin/RetrievalConfigPage";

function ok<T>(data: T) {
  return { status: "ok" as const, data, error: null, meta: { trace_id: "", elapsed_ms: 0 } };
}

const BASE_CONFIG = {
  vector_enabled: true,
  vector_weight: 0.5,
  bm25_enabled: true,
  bm25_weight: 0.5,
  top_k: 4,
  candidate_k: 20,
  rrf_k: 60,
  rerank_enabled: true,
  rerank_top_n: 8,
  ratelimit_per_min: 60,
  ratelimit_capacity: 30,
};

vi.mock("../src/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api-client")>();
  return {
    ...actual,
    fetchRetrievalConfig: vi.fn(async () => ok({ active: BASE_CONFIG, pending: null, effective_at: null })),
    fetchScopedConfigs: vi.fn(async () =>
      ok({
        scoped: [
          {
            scope: "tenant:acme",
            enabled: true,
            experiment_id: "exp-rank-v2",
            config: BASE_CONFIG,
          },
          {
            scope: "percent:20",
            enabled: false,
            experiment_id: "exp-hybrid",
            config: BASE_CONFIG,
          },
        ],
      }),
    ),
  };
});

describe("检索配置页 · 实验分组视图", () => {
  it("列出作用域、覆盖口径、实验标签与状态", async () => {
    render(<RetrievalConfigPage />);
    await waitFor(() => {
      expect(screen.getByTestId("scoped-configs")).toBeTruthy();
    });
    expect(screen.getByText("tenant:acme")).toBeTruthy();
    expect(screen.getByText(/定向到单个租户/)).toBeTruthy();
    expect(screen.getByText(/覆盖约 20% 的租户/)).toBeTruthy();
    expect(screen.getByText("exp-rank-v2")).toBeTruthy();
    expect(screen.getByText("生效中")).toBeTruthy();
    expect(screen.getByText("已停用")).toBeTruthy();
  });

  it("写明优先级（租户定向 > 分桶 > 全局）", async () => {
    render(<RetrievalConfigPage />);
    await waitFor(() => {
      expect(screen.getByTestId("scoped-configs")).toBeTruthy();
    });
    expect(screen.getByText(/优先级：租户定向/)).toBeTruthy();
    // 文本在多层祖先里都出现（`<p>` 与其容器）→ 用 getAllByText
    expect(screen.getAllByText(/稳定哈希/).length).toBeGreaterThan(0);
  });

  it("没有灰度覆盖时给出明确说明", async () => {
    const api = await import("../src/lib/api-client");
    vi.mocked(api.fetchScopedConfigs).mockResolvedValueOnce(ok({ scoped: [] }) as never);
    render(<RetrievalConfigPage />);
    await waitFor(() => {
      expect(screen.getByText(/当前没有灰度覆盖/)).toBeTruthy();
    });
  });

  it("作用域读不到时不让整页失败", async () => {
    const api = await import("../src/lib/api-client");
    vi.mocked(api.fetchScopedConfigs).mockRejectedValueOnce(new Error("boom") as never);
    render(<RetrievalConfigPage />);
    await waitFor(() => {
      expect(screen.getByTestId("scoped-configs")).toBeTruthy();
    });
    // 配置参数区仍然可用
    expect(screen.getByText("检索参数配置")).toBeTruthy();
    expect(screen.getByText(/当前没有灰度覆盖/)).toBeTruthy();
  });
});
