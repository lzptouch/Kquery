/**
 * `Retry-After` 解析（`C-06` 背压提示的唯一真相源）。
 *
 * ## 为什么要有这个模块
 *
 * 429（限流）与 503（背压）**不是故障**，是「稍后再来」——
 * 但此前只有 `sse-client.ts` 认识它们（非流式请求走 `api-client.ts`，
 * 一律被折成「请求失败（HTTP 503）」，用户看不到任何可操作信息）。
 *
 * 两处各写一份解析会漂移，所以抽到这里：`sse-client` 与 `api-client` **同源**。
 *
 * ## 为什么要保守缺省
 *
 * `Retry-After` 可能缺失、非数字、或是一个 HTTP 日期。
 * 解析失败时给 **5 秒**（不是 0、也不是无限）：
 * - 给 0 → 用户立刻重试 → 再次触发限流 → 打死马虎（限流器最怕这个）；
 * - 给一个很大的值 → 用户干等 → 体感像卡死；
 * - 5 秒 = 后端 `raise_overloaded()` 的默认退避值，与服务端口径一致。
 */

/** 默认退避秒数（与后端 `raise_overloaded()` 的 `retry_after=5` 对齐）。 */
export const DEFAULT_RETRY_AFTER_SECONDS = 5;

/** 能「稍后再来」的状态码：限流与背压。 */
export const BACKPRESSURE_STATUSES = [429, 503] as const;

/** 该状态码是否属于背压（可重试）。 */
export function isBackpressureStatus(status: number): boolean {
  return (BACKPRESSURE_STATUSES as readonly number[]).includes(status);
}

/**
 * 从响应头解析 `Retry-After`（秒数）。
 *
 * - 缺失 / 非法 → `DEFAULT_RETRY_AFTER_SECONDS`；
 * - 负数或 0 → 同样回落默认值（0 会导致立即重试，反而加剧限流）；
 * - 上限 300 秒：避免服务端写了个 86400 让前端干等一天。
 */
export function parseRetryAfter(headers: Headers | null | undefined): number {
  const raw = headers?.get?.("Retry-After");
  if (raw == null) return DEFAULT_RETRY_AFTER_SECONDS;
  const seconds = Number(String(raw).trim());
  if (!Number.isFinite(seconds) || seconds <= 0) return DEFAULT_RETRY_AFTER_SECONDS;
  return Math.min(Math.floor(seconds), 300);
}

/** 背压场景的默认文案（与 `sse-client` 保持一致）。 */
export function backpressureMessage(status: number): string {
  return status === 429 ? "请求过于频繁，请稍后重试" : "服务繁忙，请稍后重试";
}

/**
 * **409「正在处理中」**（`MP-03`）。
 *
 * ## 为什么单列
 *
 * 409 与 429 / 503 **语义不同**：
 * - 429 / 503 = 「系统忙，等一会儿再来」（背压）；
 * - 409 = **「同一个问题正在处理中」**（在飞去重，`services/idempotency.py`）——
 *   它不是故障，重试也不会更快，**等前一个请求出结果才是正解**。
 *
 * 前端此前把 409 折成「请求失败（HTTP 409）」，用户看到的像是系统坏了。
 */

/** 「正在处理中」的状态码。 */
export const IN_PROGRESS_STATUS = 409;

/** 该状态码是否属于「正在处理中」。 */
export function isInProgressStatus(status: number): boolean {
  return status === IN_PROGRESS_STATUS;
}

/** 「正在处理中」的默认文案（与后端 `REQUEST_IN_PROGRESS` 的语义一致）。 */
export function inProgressMessage(): string {
  return "同一问题正在处理中，请稍候再试";
}
