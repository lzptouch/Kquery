/**
 * 身份注入（TASK-160 前端配套）。
 *
 * ## 为什么需要这个文件
 *
 * 后端在 TASK-160 之后给全部 28 个 `/admin/*` 端点加了 `require_role("admin")`，
 * 其余端点加了 `get_principal`。但前端所有请求都是裸 `fetch(path)`，不带任何凭证——
 * 结果是**管理页全部 403**，用户看到的是"功能坏了"，而不是"没登录"。
 *
 * 这是后端鉴权改造的必然连带影响（文档附录 D 已预判：需要同步改前端）。
 *
 * ## 两种身份来源
 *
 * | 模式 | 后端配置 | 前端发送 |
 * | --- | --- | --- |
 * | 本地开发 | `AUTH_ENABLED=false`（后端信任身份头） | `X-User-Id` / `X-Roles` / `X-Tenant-Id` |
 * | 生产 | `AUTH_ENABLED=true`（校验签名令牌） | `Authorization: Bearer <token>` |
 *
 * ## 安全边界（重要）
 *
 * 身份头模式**只在后端 `AUTH_ENABLED=false` 时有效**——后端此时信任客户端自报身份，
 * 属明确标注的开发形态。生产必须走 Bearer 令牌，此时本模块只负责带上令牌，
 * 不做任何身份推断。因此这里存的身份**不是**权限依据，只是开发期的便利开关。
 *
 * 身份持久化在 `localStorage`，键前缀 `kquery.`，便于运维/测试用脚本切换身份
 * 而不必改代码（浏览器 E2E 正是靠这一点验证越权路径）。
 */
import type { Principal } from "../types";

const K = {
  userId: "kquery.user_id",
  tenantId: "kquery.tenant_id",
  roles: "kquery.roles",
  token: "kquery.token",
} as const;

/** 本地开发默认身份：与后端 `AUTH_ENABLED=false` 的信任模型配套。 */
const DEV_DEFAULTS = { userId: "admin", tenantId: "default", roles: "admin" };

/**
 * 读身份项。返回 `null` 表示**未设置**（用默认身份），空串表示**显式设为空**。
 *
 * 必须区分这两种情况：`roles` 的空串是合法且有意义的取值（"没有任何角色"），
 * 若用 `|| 默认值` 兜底，就**无法表达"非管理员"身份**——而这恰恰是
 * 测试越权路径、运维降权验证时最需要的开关（浏览器 E2E 撞到过这个坑）。
 */
function readRaw(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    // 隐私模式 / SSR 下 localStorage 不可用：退化为默认身份，不抛异常
    return null;
  }
}

function read(key: string): string {
  return readRaw(key) ?? "";
}

function write(key: string, value: string): void {
  try {
    if (value) localStorage.setItem(key, value);
    else localStorage.removeItem(key);
  } catch {
    /* 存不进去也不影响本次请求 */
  }
}

/** 当前身份（令牌优先：有令牌时后端只认令牌，身份头无意义）。 */
export function currentPrincipal(): Principal {
  const token = read(K.token);
  // 未设置（null）才用默认值；显式设为空串是"我就是要没有角色/没有身份"，
  // 必须原样透传，否则越权断言永远测不出来（空串会被兜底成 admin）
  const pick = (key: string, fallback: string): string => {
    const v = readRaw(key);
    return v === null ? fallback : v;
  };
  return {
    user_id: pick(K.userId, DEV_DEFAULTS.userId),
    tenant_id: pick(K.tenantId, DEV_DEFAULTS.tenantId),
    roles: pick(K.roles, DEV_DEFAULTS.roles),
    token,
  };
}

export function setPrincipal(patch: Partial<Principal>): void {
  if (patch.user_id !== undefined) write(K.userId, patch.user_id);
  if (patch.tenant_id !== undefined) write(K.tenantId, patch.tenant_id);
  if (patch.roles !== undefined) write(K.roles, patch.roles);
  if (patch.token !== undefined) write(K.token, patch.token);
}

export function clearPrincipal(): void {
  Object.values(K).forEach((k) => write(k, ""));
}

/**
 * 构造认证头。所有出站请求（fetch / XHR / SSE）都必须经过它。
 *
 * 单独抽成函数而不是在 fetch 里内联，是为了**保证新增请求不会漏**——
 * 漏一处的症状是"某个页面单独 403"，很难联想到鉴权。
 */
export function authHeaders(): Record<string, string> {
  const p = currentPrincipal();
  if (p.token) {
    return { Authorization: `Bearer ${p.token}` };
  }
  return {
    "X-User-Id": p.user_id,
    "X-Tenant-Id": p.tenant_id,
    "X-Roles": p.roles,
  };
}

/** 合并调用方自带的头（调用方显式指定的优先级更高）。 */
export function withAuth(headers?: HeadersInit): Record<string, string> {
  return { ...authHeaders(), ...(headers as Record<string, string> | undefined) };
}

/**
 * 鉴权失败的可读提示。
 *
 * 401/403 在界面上必须与"服务挂了"区分开：前者是身份问题（改身份/登录即可），
 * 后者是系统问题（等运维）。混在一起会让用户反复刷新而毫无进展。
 */
export function authErrorMessage(status: number): string {
  if (status === 401) return "未认证：请提供有效凭证后重试";
  if (status === 403) return "无权限：当前身份无权访问该功能（需要管理员角色）";
  return "";
}

export function isAuthFailure(status: number): boolean {
  return status === 401 || status === 403;
}
