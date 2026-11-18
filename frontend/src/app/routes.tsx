/** 应用层路由表。
 *
 * 路由只做页面组装与布局，不写展示细节；
 * 页面组件从各 feature 的公开出口导入，features 之间禁止互相 import。
 *
 * 除对话页（首屏必经）外全部 `React.lazy`：页面里绝大多数是低频管理页，
 * 全静态 import 会把它们一起打进首屏 bundle，直接违反 LCP ≤2.0s / TTI ≤3.0s 预算。
 *
 * ## 认证与授权已下线（2026-11-12）
 *
 * 原先这里有两层守卫：`RequireAuth`（未登录跳 /login）与 `RequireAdmin`
 * （非管理员渲染无权页），登录/注册页各自挂在无布局的分支上。现在后端不再校验
 * 凭证与角色，两层守卫与登录/注册路由一并删除——**所有页面直接可达**。
 *
 * 访问边界改由部署层承担（反向代理 / 内网 / 网关），应用不再自建身份体系。
 */
import { lazy } from "react";
import { Route, Routes, useLocation } from "react-router-dom";
import { ChatPage } from "../features/chat";
import { NotFoundPage } from "../features/common";
import { AppLayout } from "../features/shell";

const MonitorPage = lazy(() => import("../features/admin").then((m) => ({ default: m.MonitorPage })));
const RetrievalConfigPage = lazy(() => import("../features/admin").then((m) => ({ default: m.RetrievalConfigPage })));
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
      <Route element={<AppLayout />}>
        {/* 对话：首屏 */}
        <Route path="/" element={<ChatPage />} />

        {/* 知识库 */}
        <Route path="/knowledge" element={<KnowledgePage />} />
        <Route path="/knowledge/upload-advanced" element={<UploadWizardPage />} />
        <Route path="/knowledge/tasks/:taskId" element={<TaskProgressPage />} />
        <Route path="/knowledge/preview/:docId" element={<PreviewPage />} />
        <Route path="/knowledge/query-analysis" element={<QueryAnalysisPage />} />

        {/* 管理区 */}
        <Route path="/admin/monitor" element={<MonitorPage />} />
        <Route path="/admin/retrieval-config" element={<RetrievalConfigPage />} />

        {/* 404 兜底：与其它页面共享布局，输错地址时带着导航一起提示，
            而不是暴露一个没有导航的孤立页面 */}
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
