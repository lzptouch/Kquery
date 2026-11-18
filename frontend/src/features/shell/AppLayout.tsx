/**
 * 应用布局壳：顶栏 + 侧边栏 + 内容区。
 *
 * 认证与授权已下线（2026-11-12）：本组件原先依赖 `useAuth()` 渲染顶栏用户信息、
 * 角色徽标与「退出登录」按钮，并把管理区整块按 `isAdmin` 隐藏。现在身份固定、
 * 管理面不再校验角色，因此：
 * - 顶栏只保留产品名与副标题（没有可展示的用户/角色概念）；
 * - 管理区导航**始终渲染**。
 *
 * 组件从 `features/auth/` 迁到 `features/shell/`：原目录名已名不副实。
 */
import { Suspense } from "react";
import { NavLink, Outlet } from "react-router-dom";

function RouteFallback() {
  return (
    <div className="panel" role="status" aria-live="polite">
      页面加载中…
    </div>
  );
}

export function AppLayout() {
  return (
    <div className="layout">
      <header className="topbar">
        <span className="logo">Kquery</span>
        <span className="topbar-subtitle">智能知识库问答</span>
      </header>
      <div className="layout-body">
        <nav className="sidebar" aria-label="主导航">
          <div className="nav-section">主功能</div>
          <NavLink to="/" end>
            对话
          </NavLink>

          <div className="nav-section">管理区</div>
          <NavLink to="/knowledge">知识库</NavLink>
          <NavLink to="/admin/monitor">监控大盘</NavLink>
          <NavLink to="/admin/retrieval-config">检索配置</NavLink>
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
