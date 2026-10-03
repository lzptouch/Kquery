/** 应用层路由表 + 布局壳（阶段 0 §2 信息架构）。
 *
 * 路由只做页面组装与布局，不写展示细节；
 * 页面组件从各 feature 的公开出口导入，features 之间禁止互相 import。
 */
import { NavLink, Route, Routes } from "react-router-dom";
import { MonitorPage, RetrievalConfigPage } from "../features/admin";
import { AclPage, AuditPage, EvalPage, QuotaPage, SensitivePage, UsersPage } from "../features/admin/security";
import { ChatPage } from "../features/chat";
import { GraphPage } from "../features/graph";
import { KnowledgePage, PreviewPage, QueryAnalysisPage, TaskProgressPage, UploadWizardPage } from "../features/knowledge";
import { ReportDetailPage, ReportsPage } from "../features/reports";

export function AppRoutes() {
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
          <Routes>
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
        </main>
      </div>
    </div>
  );
}
