/**
 * 路由守卫与布局壳。
 *
 * ## 为什么 RequireAdmin 渲染页面而不是重定向
 *
 * 静默跳回首页的话，用户深链 `/admin/users` 后看到的是"被弹回来了"，
 * 既不知道发生了什么，也无从判断该找谁要权限。而后端对同一请求会回
 * 403 + "无权限或不存在"——两者对不上，人只会怀疑是系统坏了。
 * 直接渲染"无权访问"并给出可执行的出口，与后端口径一致。
 */
import { Navigate, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";

function Waiting({ label }: { label: string }) {
  return (
    <div className="panel" role="status" aria-live="polite">
      {label}
    </div>
  );
}

/** 要求已登录。未登录 → 登录页，并记住来路以便登录后原路返回。 */
export function RequireAuth() {
  const { session, ready } = useAuth();
  const location = useLocation();
  if (!ready) return <Waiting label="正在校验登录状态…" />;
  if (!session) return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  return <Outlet />;
}

/** 要求 admin 角色。已登录但无角色 → 渲染"无权访问"页（不跳转，见文件头说明）。 */
export function RequireAdmin() {
  const { session, ready, isAdmin } = useAuth();
  const location = useLocation();
  if (!ready) return <Waiting label="正在校验登录状态…" />;
  if (!session) return <Navigate to="/login" state={{ from: location.pathname }} replace />;
  if (!isAdmin) {
    return (
      <div className="empty-state" role="alert">
        <p className="empty-title">无权访问</p>
        <p>该功能属于管理区，仅管理员可用。</p>
        <p className="muted">当前账号：{session.user_id}（角色：{session.roles.join("、") || "无"}）</p>
        <p className="muted">如需权限请联系管理员在「用户权限」中分配。</p>
      </div>
    );
  }
  return <Outlet />;
}

/** 登录/注册页的守卫：已登录时不该再看到登录框，直接进首页。 */
export function RedirectIfAuthenticated({ children }: { children: React.ReactNode }) {
  const { session, ready } = useAuth();
  if (!ready) return <Waiting label="正在校验登录状态…" />;
  if (session) return <Navigate to="/" replace />;
  return <>{children}</>;
}
