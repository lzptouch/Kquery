/** 应用层路由表。
 *
 * 路由只做页面组装与布局，不写展示细节；
 * 页面组件从各 feature 的公开出口导入，features 之间禁止互相 import。
 *
 * 除对话页（首屏必经）外全部 `React.lazy`：14 个页面里有 13 个是低频管理页，
 * 全静态 import 会把它们一起打进首屏 bundle，直接违反  的
 * LCP ≤2.0s / TTI ≤3.0s 预算。
 *
 * ## 为什么是嵌套路由而不是逐页包守卫
 *
 * 管理区有 13 条路由。逐条写 `<RequireAdmin><XxxPage/></RequireAdmin>` 既冗长，
 * 又容易在新增页面时漏包一个——漏包的后果是该页面对普通用户可见
 * （后端仍会 403，但界面已经给出了错误的预期）。用一个无路径的布局路由统一收口，
 * 新增管理页只需放进 `RequireAdmin` 那一层，守卫在结构上无法被遗漏。
 *
 * ## 布局为什么也要拆出去
 *
 * 登录/注册页不能有侧边栏。若保留原先"layout 硬编码包在 `<Routes>` 外"的结构，
 * 就无法让个别路由逃出布局——故布局移入 `features/auth/AppLayout`，经 `<Outlet/>` 承载内容。
 */
import { lazy } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { ChatPage } from "../features/chat";
import {
  AppLayout,
  LoginPage,
  NotFoundPage,
  RedirectIfAuthenticated,
  RegisterPage,
  RequireAdmin,
  RequireAuth,
} from "../features/auth";

const MonitorPage = lazy(() => import("../features/admin").then((m) => ({ default: m.MonitorPage })));
const RetrievalConfigPage = lazy(() => import("../features/admin").then((m) => ({ default: m.RetrievalConfigPage })));
const UsersPage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.UsersPage })));
const AclPage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.AclPage })));
const KbPage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.KbPage })));
const KnowledgePage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.KnowledgePage })));
const PreviewPage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.PreviewPage })));
const QueryAnalysisPage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.QueryAnalysisPage })));
const TaskProgressPage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.TaskProgressPage })));
const UploadWizardPage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.UploadWizardPage })));

export function AppRoutes() {
  // 显式传入 location：切路由时让 <Routes> 按新位置重新匹配，
  // 否则上一页的局部状态可能泄漏到下一页（沿用原实现的原因）
  const location = useLocation();
  return (
    <Routes location={location}>
      {/* 免布局、免守卫：登录/注册页不该出现侧边栏，已登录时也不该再看到登录框 */}
      <Route path="/login" element={<RedirectIfAuthenticated><LoginPage /></RedirectIfAuthenticated>} />
      <Route path="/register" element={<RedirectIfAuthenticated><RegisterPage /></RedirectIfAuthenticated>} />

      <Route element={<RequireAuth />}>
        <Route element={<AppLayout />}>
          {/* 对话：所有登录用户唯一可用的页面 */}
          <Route path="/" element={<ChatPage />} />

          {/* 管理区：与后端 require_role("admin") 逐个 router 对应 */}
          <Route element={<RequireAdmin />}>
            <Route path="/knowledge" element={<KnowledgePage />} />
            <Route path="/knowledge/upload-advanced" element={<UploadWizardPage />} />
            <Route path="/knowledge/tasks/:taskId" element={<TaskProgressPage />} />
            <Route path="/knowledge/preview/:docId" element={<PreviewPage />} />
            <Route path="/knowledge/query-analysis" element={<QueryAnalysisPage />} />
            <Route path="/admin/monitor" element={<MonitorPage />} />
            <Route path="/admin/retrieval-config" element={<RetrievalConfigPage />} />
            <Route path="/admin/users" element={<UsersPage />} />
            <Route path="/admin/acl" element={<AclPage />} />
            <Route path="/admin/kb" element={<KbPage />} />
          </Route>

          {/* 404 兜底：置于已登录层内，输错地址时会带着布局一起提示，
              而不是暴露一个没有导航的孤立页面 */}
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
