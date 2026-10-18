/**
 * 应用布局壳：顶栏 + 侧边栏 + 内容区。
 *
 * 从 `app/routes.tsx` 抽出成独立组件，是因为登录/注册页必须**没有**侧边栏——
 * 原先 layout 是硬编码包在 `<Routes>` 外层的，登录页无论怎么加都会被塞进侧边栏里。
 */
import { Suspense } from "react";
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuth } from "./AuthContext";

function RouteFallback() {
  return (
    <div className="panel" role="status" aria-live="polite">
      页面加载中…
    </div>
  );
}

export function AppLayout() {
  const { session, isAdmin, logout } = useAuth();
  const navigate = useNavigate();

  return (
    <div className="layout">
      <header className="topbar">
        <span className="logo">Kquery</span>
        <span className="topbar-subtitle">智能知识库问答</span>
        {session && (
          <div className="topbar-user">
            <span className="topbar-username" title={session.user_id}>
              {session.user_id}
            </span>
            {session.roles.length > 0 && (
              <span className="status-tag" title="当前角色">
                {session.roles.join("、")}
              </span>
            )}
            <button
              className="btn btn-text"
              type="button"
              onClick={() => {
                logout();
                navigate("/login", { replace: true });
              }}
            >
              退出登录
            </button>
          </div>
        )}
      </header>
      <div className="layout-body">
        <nav className="sidebar" aria-label="主导航">
          <div className="nav-section">主功能</div>
          <NavLink to="/" end>
            对话
          </NavLink>

          {/* 管理区整块按角色渲染：非管理员连分组标题都不出现，
              避免"看得到入口点进去才发现没权限"的二次挫败 */}
          {isAdmin && (
            <>
              <div className="nav-section">管理区</div>
              <NavLink to="/knowledge">知识库</NavLink>
              <NavLink to="/graph">图谱</NavLink>
              <NavLink to="/admin/monitor">监控大盘</NavLink>
              <NavLink to="/admin/retrieval-config">检索配置</NavLink>
              <NavLink to="/admin/users">用户权限</NavLink>
              <NavLink to="/admin/acl">文档 ACL</NavLink>
              <NavLink to="/admin/audit">审计日志</NavLink>
              <NavLink to="/admin/quota">配额管理</NavLink>
              <NavLink to="/admin/sensitive">敏感词</NavLink>
            </>
          )}
        </nav>
        <main className="main">
          <Suspense fallback={<RouteFallback />}>
            <Outlet />
          </Suspense>
        </main>
      </div>
    </div>
  );
}
