/**
 * 会话上下文：登录态的唯一 React 侧镜像。
 *
 * ## 单一事实源
 *
 * 真实状态存在 `localStorage`（`lib/auth.ts` 负责读写），本 Context 只是它的镜像。
 * 之所以不把状态只放在 Context 里：`authHeaders()` 由 `fetch`/`XHR`/`SSE` 调用，
 * 那些地方不在 React 树内，拿不到 Context。两边各存一份必然漂移——
 * 最典型的症状是"点了退出登录，管理页还能正常打开"。
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import {
  DEV_BYPASS,
  clearPrincipal,
  currentSession,
  isInvalidCredential,
  setPrincipal,
  setUnauthorizedHandler,
} from "../../lib/auth";
import { fetchMe, login as loginApi, register as registerApi } from "../../lib/api-client";
import type { Envelope, MeResponse, SessionUser } from "../../types";

interface AuthState {
  /** null = 未登录（开发旁路下不会出现 null） */
  session: SessionUser | null;
  /** 启动校验是否完成。未完成前不做路由判定，否则会闪一下登录页再跳回首页 */
  ready: boolean;
  isAdmin: boolean;
  login: (username: string, password: string) => Promise<string | null>;
  register: (username: string, password: string) => Promise<string | null>;
  logout: () => void;
  hasRole: (role: string) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  // 惰性初始化：直接从存储读一次。少了这一步，开发旁路下 session 会一直是
  // null——因为旁路分支只置 ready 而没有令牌可校验，也就永远不会调用 adopt()，
  // 表现为"改完代码刷新就掉到登录页"。
  const [session, setSession] = useState<SessionUser | null>(() => currentSession());
  const [ready, setReady] = useState(false);

  /** 把后端回传的身份写回 localStorage 并更新 Context——两者必须同时更新 */
  const adopt = useCallback((next: SessionUser) => {
    setPrincipal({
      user_id: next.user_id,
      tenant_id: next.tenant_id,
      roles: next.roles.join(","),
      token: next.token,
    });
    setSession(next);
  }, []);

  const logout = useCallback(() => {
    clearPrincipal();
    setSession(null);
  }, []);

  // 401 全局处理：令牌失效/账号被停用时立刻回到登录页。
  // 只处理 401 不处理 403——403 是"已登录但无权"，跳转反而掩盖真实原因。
  useEffect(() => {
    setUnauthorizedHandler(() => logout());
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  // 启动校验：本地有令牌就问一次后端"我是谁"。
  // 不校验的代价是：令牌已过期时用户会一路走到某个页面才撞上 401，
  // 体验上像"功能坏了"；而且管理员刚把该账号停用时前端毫无感知。
  useEffect(() => {
    const stored = currentSession();
    if (!stored) {
      setReady(true);
      return;
    }
    if (!stored.token && DEV_BYPASS) {
      // 开发旁路：有身份但没有令牌，说明是本地默认身份而非登录所得。
      // 此处**不**去问 /auth/me——旁路的意义就是"不用登录"，让它依赖一次
      // 服务端往返会带来两个坏处：每次刷新都多一个请求；以及后端版本
      // 与前端不一致时（比如后端还没重启），整个开发环境会直接卡在登录页。
      setReady(true);
      return;
    }
    let cancelled = false;
    fetchMe().then((env: Envelope<MeResponse>) => {
      if (cancelled) return;
      if (env.status === "ok" && env.data) {
        // 身份以服务端为准（角色可能已被管理员改过），但令牌沿用本地那份——
        // /auth/me 只回答"我是谁"，不重新下发令牌
        adopt({
          user_id: env.data.user_id,
          tenant_id: env.data.tenant_id,
          roles: env.data.roles,
          token: stored.token,
        });
      } else if (isInvalidCredential(env.error?.code)) {
        // 只有"凭证确实无效/过期"才登出。
        // 限流（429）、服务异常（5xx）、网关错误都不等于身份失效——
        // 那些情况登出会把用户踢回登录页，还得重新输一遍口令，
        // 而根因（后端在抖/被限流）一个都没解决。
        logout();
      } else {
        // 网络异常不等于身份无效：保留本地会话，让用户继续用并在下次请求时再判定。
        if (!cancelled) adopt(stored);
      }
    }).catch(() => {
      // 网络异常不等于身份无效：保留本地会话，让用户继续用并在下次请求时再判定。
      // 这里若一并登出，后端短暂抖动会把所有人踢回登录页。
      if (!cancelled) adopt(stored);
    }).finally(() => {
      if (!cancelled) setReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, [adopt, logout]);

  const login = useCallback(async (username: string, password: string): Promise<string | null> => {
    const env = await loginApi(username, password);
    if (env.status !== "ok" || !env.data) return env.error?.message ?? "登录失败";
    adopt({
      user_id: env.data.user_id,
      tenant_id: env.data.tenant_id,
      roles: env.data.roles,
      token: env.data.token,
    });
    return null;
  }, [adopt]);

  const register = useCallback(async (username: string, password: string): Promise<string | null> => {
    const env = await registerApi(username, password);
    if (env.status !== "ok" || !env.data) return env.error?.message ?? "注册失败";
    return null;
  }, []);

  const value = useMemo<AuthState>(() => ({
    session,
    ready,
    isAdmin: session?.roles.includes("admin") ?? false,
    login,
    register,
    logout,
    hasRole: (role: string) => session?.roles.includes(role) ?? false,
  }), [session, ready, login, register, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return ctx;
}
