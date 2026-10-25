/** 前端埋点（）。
 *
 * 五类事件：page_view / interaction / error / streaming / client_metric。
 * 此前这五类**全部零实现**，导致 §10.1 的 LCP/TTI/首 token/路由切换四个性能预算
 * 没有任何数据可验证——"可观测先行"这条设计原则在客户端侧完全落空。
 *
 * 三条纪律：
 * 1. **不记录内容**：问题、答案、文档名一律不进埋点（合规红线，与后端审计一致）；
 * 2. **失败不影响业务**：sendBeacon 抛错只写 console.debug，绝不打断交互；
 * 3. **可关闭**：REACT_APP_TELEMETRY_DISABLED 或 localStorage 开关关闭后完全静默。
 */

export type TelemetryEvent =
  | "page_view"
  | "interaction"
  | "error"
  | "streaming"
  | "client_metric";

export interface TelemetryPayload {
  event: TelemetryEvent;
  /** 路由路径（page_view） */
  path?: string;
  /** 交互名（interaction）：只记动作，不记参数内容 */
  action?: string;
  /** 错误码 / message（error） */
  code?: number | string;
  message?: string;
  /** 数值指标（client_metric / streaming） */
  metric?: string;
  value?: number;
  /** 附加标量维度 */
  extra?: Record<string, string | number | boolean>;
}

type Sink = (payload: TelemetryPayload) => void;

const DISABLED_KEY = "kquery.telemetry.disabled";
/** 最近事件环形缓冲（导出仅用于调试与自测，生产无消费者）。 */
const buffer: TelemetryPayload[] = [];
const MAX_BUFFER = 200;

function disabled(): boolean {
  const env = (import.meta as { env?: Record<string, string | undefined> }).env;
  if (env?.VITE_TELEMETRY_DISABLED === "true") return true;
  try {
    return localStorage.getItem(DISABLED_KEY) === "1";
  } catch {
    return false;
  }
}

/** 注册上报出口。默认走 console（开发期可见），生产可换成 HTTP 上报。 */
let sink: Sink = (payload) => {
  // eslint-disable-next-line no-console
  console.debug("[telemetry]", payload.event, payload);
};

export function setTelemetrySink(next: Sink): void {
  sink = next;
}

export function track(payload: TelemetryPayload): void {
  if (disabled()) return;
  try {
    buffer.push(payload);
    if (buffer.length > MAX_BUFFER) buffer.shift();
    sink(payload);
  } catch (err) {
    // 埋点失败绝不能影响用户操作
    // eslint-disable-next-line no-console
    console.debug("[telemetry] sink 失败", err);
  }
}

export function pageView(path: string): void {
  track({ event: "page_view", path });
}

export function interaction(action: string, extra?: TelemetryPayload["extra"]): void {
  track({ event: "interaction", action, extra });
}

export function reportError(code: string | number, message: string, extra?: TelemetryPayload["extra"]): void {
  track({ event: "error", code, message: message.slice(0, 200), extra });
}

/** 流式指标：总耗时 / 首 token 延迟 / token 总数。 */
export function streamingStat(metric: string, value: number, extra?: TelemetryPayload["extra"]): void {
  track({ event: "streaming", metric, value, extra });
}

export function clientMetric(metric: string, value: number, extra?: TelemetryPayload["extra"]): void {
  track({ event: "client_metric", metric, value, extra });
}

export function setTelemetryEnabled(enabled: boolean): void {
  try {
    localStorage.setItem(DISABLED_KEY, enabled ? "0" : "1");
  } catch {
    /* 隐私模式下写入失败：忽略，不影响功能 */
  }
}

export function isTelemetryEnabled(): boolean {
  return !disabled();
}

// ---- 全局接线 ----

let installed = false;

/** 由 main.tsx 调用一次：接管路由切换埋点、JS 异常与资源计时。 */
export function installTelemetry(): () => void {
  if (installed) return () => undefined;
  installed = true;

  // ① page_view：History API 监听（不依赖具体路由实现）
  const onPopState = () => pageView(location.pathname);
  addEventListener("popstate", onPopState);
  pageView(location.pathname);

  // ② error：JS 运行时异常与 Promise 未捕获拒绝
  const onError = (event: ErrorEvent) =>
    reportError("js_error", event.message, { file: event.filename, line: event.lineno });
  const onRejection = (event: PromiseRejectionEvent) =>
    reportError("unhandled_rejection", String(event.reason));
  addEventListener("error", onError);
  addEventListener("unhandledrejection", onRejection);

  // ③ client_metric：LCP / FCP / TTFB 等资源时长
  let observer: PerformanceObserver | null = null;
  if (typeof PerformanceObserver !== "undefined") {
    try {
      observer = new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          const duration = (entry as PerformanceEntry & { duration?: number }).duration ?? 0;
          clientMetric(entry.name, Math.round(entry.startTime), {
            duration: Math.round(duration),
          });
        }
      });
      observer.observe({ type: "paint", buffered: true });
      observer.observe({ type: "largest-contentful-paint", buffered: true });
    } catch {
      // 浏览器不支持该 entry 类型：跳过，不影响其它指标
      observer = null;
    }
  }

  return () => {
    installed = false;
    removeEventListener("popstate", onPopState);
    removeEventListener("error", onError);
    removeEventListener("unhandledrejection", onRejection);
    observer?.disconnect();
  };
}

/** 导出缓冲区仅用于调试与自测：读取后清空。 */
export function drainLocalBuffer(): TelemetryPayload[] {
  return buffer.splice(0, buffer.length);
}