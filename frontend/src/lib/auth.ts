/**
 * 身份注入与登录会话（ 前端配套 + 账号密码登录档）。
 *
 * ## 两种身份来源
 *
 * | 模式 | 后端配置 | 前端发送 |
 * | --- | --- | --- |
 * | 本地开发 | `AUTH_ENABLED=false`（后端信任身份头） | `X-User-Id` / `X-Roles` / `X-Tenant-Id` |
 * | 生产 | `AUTH_ENABLED=true`（校验签名令牌） | `Authorization: Bearer <token>` |
 *
 * ## 开发旁路为什么用编译期常量
 *
 * 早期版本让任何未设置 localStorage 的人都是 admin——那等于没有门禁：
 * 清一下缓存就进管理区。而用 `import.meta.env.DEV` 做开关后，
 * `npm run dev`（后端 `AUTH_ENABLED=false`）仍然开箱即用，
 * `npm run build` 产出的东西则**没有**任何默认身份，必须真实登录。
 * 关键是它由构建期常量决定：运行时无法被 localStorage 打开，
 * 因此不存在"改个存储键就绕过登录"的路径。
 *
 * 注意 `scripts/e2e_browser.py` 打的是构建产物（默认 :8081），
 * `import.meta.env.DEV` 为 false，所以 E2E 必须走真实登录流程。
 *
 * ## 安全边界（重要）
 *
 * 身份头模式**只在 `import.meta.env.DEV` 且后端 `AUTH_ENABLED=false` 时才发送**，
 * 属明确标注的开发形态。生产一律走 Bearer 令牌，本模块只负责带上令牌，
 * 不做任何身份推断。因此这里存的身份**不是**权限依据，只是开发期的便利开关。
 */
import type { Principal, SessionUser } from "../types";

const K = {
  userId: "kquery.user_id",
  tenantId: "kquery.tenant_id",
  roles: "kquery.roles",
  token: "kquery.token",
} as const;

/** 开发旁路：仅构建期 dev server 为真，生产构建恒为 false。 */
export const DEV_BYPASS = import.meta.env.DEV;

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
  if (DEV_BYPASS) {
    return {
      "X-User-Id": p.user_id,
      "X-Tenant-Id": p.tenant_id,
      "X-Roles": p.roles,
    };
  }
  // 生产构建且尚未登录：一个身份头都不发。
  // 发空身份头会让后端的 HeaderAuthProvider 档（若被误开）把空串当成合法身份，
  // 凭空造出一个"没有角色但已认证"的主体——比不发更糟。
  return {};
}

/** 把逗号分隔的角色串解析成数组；空串 → 空数组（"显式没有任何角色"是合法语义）。 */
export function rolesOf(principal: Principal): string[] {
  return principal.roles
    .split(",")
    .map((r) => r.trim())
    .filter(Boolean);
}

/**
 * 当前会话；未登录返回 `null`。
 *
 * 唯一的事实源是 localStorage——`authHeaders()` 也从那里读。两处若各存一份，
 * 迟早会漂移（登出清了存储但 Context 还以为已登录），症状是"登出后仍能点管理页"。
 */
export function currentSession(): SessionUser | null {
  const token = read(K.token);
  if (token) {
    const p = currentPrincipal();
    return {
      user_id: p.user_id,
      tenant_id: p.tenant_id,
      roles: rolesOf(p),
      token,
    };
  }
  if (DEV_BYPASS) {
    return {
      user_id: DEV_DEFAULTS.userId,
      tenant_id: DEV_DEFAULTS.tenantId,
      roles: DEV_DEFAULTS.roles.split(","),
      token: "",
    };
  }
  return null;
}

/** 是否具备指定角色。登录态以 `/auth/me` 回填的 roles 为准。 */
export function hasRole(roles: string[], role: string): boolean {
  return roles.includes(role);
}

/**
 * 认证失效（401）的全局回调注册。
 *
 * 放在 `auth.ts` 而不是 `api-client.ts`：`api-client` 与 `sse-client` 都要用它，
 * 而它们彼此没有理由互相依赖。挂在三者共同依赖的最底层，才不会为共享一个
 * 回调而引入新的模块边。
 *
 * 只处理 401，不处理 403——403 表示"已登录但无权"，跳转会把用户踢出登录，
 * 反而掩盖真实原因。
 */
let unauthorizedHandler: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function notifyUnauthorized(): void {
  unauthorizedHandler?.();
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
