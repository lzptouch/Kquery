/** 应用层路由表 + 布局壳（阶段 0 §2 信息架构）。
 *
 * 路由只做页面组装与布局，不写展示细节；
 * 页面组件从各 feature 的公开出口导入，features 之间禁止互相 import。
 *
 * 除对话页（首屏必经）外全部 `React.lazy`：17 个页面里有 16 个是低频管理页，
 * 全静态 import 会把它们一起打进首屏 bundle，直接违反 stage-0 §10.1 的
 * LCP ≤2.0s / TTI ≤3.0s 预算。
 */
import { Suspense, lazy } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import { ChatPage } from "../features/chat";

const MonitorPage = lazy(() => import("../features/admin").then((m) => ({ default: m.MonitorPage })));
const RetrievalConfigPage = lazy(() => import("../features/admin").then((m) => ({ default: m.RetrievalConfigPage })));
const UsersPage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.UsersPage })));
const AclPage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.AclPage })));
const AuditPage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.AuditPage })));
const QuotaPage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.QuotaPage })));
const SensitivePage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.SensitivePage })));
const EvalPage = lazy(() => import("../features/admin/security").then((m) => ({ default: m.EvalPage })));
const GraphPage = lazy(() => import("../features/graph").then((m) => ({ default: m.GraphPage })));
const KnowledgePage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.KnowledgePage })));
const PreviewPage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.PreviewPage })));
const QueryAnalysisPage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.QueryAnalysisPage })));
const TaskProgressPage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.TaskProgressPage })));
const UploadWizardPage = lazy(() => import("../features/knowledge").then((m) => ({ default: m.UploadWizardPage })));
const ReportsPage = lazy(() => import("../features/reports").then((m) => ({ default: m.ReportsPage })));
const ReportDetailPage = lazy(() => import("../features/reports").then((m) => ({ default: m.ReportDetailPage })));

/** 路由级兜底：懒加载 chunk 拉取中/失败时的占位。
 *  与页面内的 loading 态区分：这里的文案说明"页面还没到"，避免与"数据为空"混淆。 */
function RouteFallback() {
  return (
    <div className="panel" role="status" aria-live="polite">
      页面加载中…
    </div>
  );
}

export function AppRoutes() {
  const location = useLocation();
  return (
    <div className="layout">
      <header className="topbar">
        <span className="logo">Kquery</span>
        <span style={{ color: "var(--text-secondary)" }}>智能知识库问答 · 阶段 9</span>
      </header>
      <div className="layout-body">
        <nav className="sidebar" aria-label="主导航">
          <div className="nav-section">主功能</div>
          <NavLink to="/" end>
            对话
          </NavLink>
          <NavLink to="/knowledge">知识库</NavLink>
          <NavLink to="/graph">图谱</NavLink>
          <NavLink to="/reports">报告中心</NavLink>
          <div className="nav-section">管理区</div>
          <NavLink to="/admin/monitor">监控大盘</NavLink>
          <NavLink to="/admin/retrieval-config">检索配置</NavLink>
          <div className="nav-section">安全治理</div>
          <NavLink to="/admin/users">用户权限</NavLink>
          <NavLink to="/admin/acl">文档 ACL</NavLink>
          <NavLink to="/admin/audit">审计日志</NavLink>
          <NavLink to="/admin/quota">配额管理</NavLink>
          <NavLink to="/admin/sensitive">敏感词</NavLink>
          <NavLink to="/admin/eval">评测平台</NavLink>
        </nav>
        <main className="main">
          {/* key={location.pathname}：切路由时重挂载，避免上一页的局部状态泄漏到下一页 */}
          <Suspense fallback={<RouteFallback />}>
            <Routes location={location}>
              <Route path="/" element={<ChatPage />} />
              <Route path="/knowledge" element={<KnowledgePage />} />
              <Route path="/graph" element={<GraphPage />} />
              <Route path="/reports" element={<ReportsPage />} />
              <Route path="/reports/:id" element={<ReportDetailPage />} />
              <Route path="/admin/users" element={<UsersPage />} />
              <Route path="/admin/acl" element={<AclPage />} />
              <Route path="/admin/audit" element={<AuditPage />} />
              <Route path="/admin/quota" element={<QuotaPage />} />
              <Route path="/admin/sensitive" element={<SensitivePage />} />
              <Route path="/admin/eval" element={<EvalPage />} />
              <Route path="/knowledge/upload-advanced" element={<UploadWizardPage />} />
              <Route path="/knowledge/tasks/:taskId" element={<TaskProgressPage />} />
              <Route path="/knowledge/preview/:docId" element={<PreviewPage />} />
              <Route path="/knowledge/query-analysis" element={<QueryAnalysisPage />} />
              <Route path="/admin/monitor" element={<MonitorPage />} />
              <Route path="/admin/retrieval-config" element={<RetrievalConfigPage />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  );
}
