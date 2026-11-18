/**
 * 监控大盘的「工具失败率」卡片（`MP-01`）。
 *
 * ## 为什么单测
 *
 * 工具失败率是工具可用性的**先行指标**：工具大面积失败时答案质量还没掉，
 * 但已经不该继续用这条链路了。这张卡如果算错了（分母错 / 空值变 NaN），
 * 运维会按错的数字做决策。
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MonitorPage } from "../src/features/admin/MonitorPage";

/** 造一个成功信封（页面读的是 `.data`）。 */
function ok<T>(data: T) {
  return { status: "ok" as const, data, error: null, meta: { trace_id: "", elapsed_ms: 0 } };
}

// **保留原模块的其它导出**：整个模块被替换时，页面里其它用到的函数会变成
// undefined → 调用即抛 → 整页落到「加载失败」分支（调试时踩过）。
vi.mock("../src/lib/api-client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api-client")>();
  return {
  ...actual,
  // API 返回的是**信封**（`{status, data, error, meta}`），不是裸数据——
  // 页面读的是 `.data`，裸数据会让整页落到「加载失败」分支。
  fetchMetricsSummary: vi.fn(async () => ok({
    qps: 1.2,
    error_rate: 0.02,
    p95_latency_ms: 120,
    cache_hit_rate: 0.8,
    llm_calls: 42,
    total_requests: 100,
  })),
  fetchTimeseries: vi.fn(async () => ok({ points: [] })),
  fetchTopQueries: vi.fn(async () => ok([])),
  fetchQualityMetrics: vi.fn(async () => ok(null)),
  fetchGraphStats: vi.fn(async () => ok({ entities: 0, relations: 0, communities: 0, images: 0 })),
  fetchAgentMetrics: vi.fn(async () => ok({
    runs: 10,
    avg_iterations: 2.5,
    fuse_rate: 0,
    tool_distribution: { search: 8, calc: 2 },
    tool_calls_total: 10,
    tool_failures: 3,
    tool_failure_rate: 0.3,
    tool_failure_distribution: { search: 3 },
    abnormal: [],
  })),
  };
});

describe("监控大盘 · 工具失败率卡片", () => {
  it("展示工具失败率与分子分母", async () => {
    render(<MonitorPage />);
    await waitFor(() => {
      expect(screen.getByText("工具失败率")).toBeTruthy();
    });
    expect(screen.getByText("30.0%")).toBeTruthy();
    expect(screen.getByText(/3 失败 \/ 10 次调用/)).toBeTruthy();
  });

  it("没有工具调用时显示 0 而不是 NaN", async () => {
    const api = await import("../src/lib/api-client");
    vi.mocked(api.fetchAgentMetrics).mockResolvedValueOnce(
      ok({
        runs: 1,
        avg_iterations: 1,
        fuse_rate: 0,
        tool_distribution: {},
        abnormal: [],
      }) as never,
    );
    render(<MonitorPage />);
    await waitFor(() => {
      expect(screen.getByText("工具失败率")).toBeTruthy();
    });
    expect(screen.queryByText(/NaN/)).toBeNull();
  });
});
