/** 根组件：全局组装。 */
import { useLocation } from "react-router-dom";
import { AppRoutes } from "./app/routes";
import { ErrorBoundary } from "./components/ErrorBoundary";

export default function App() {
  const location = useLocation();
  return (
    // `key` 用 pathname：出错后用户点侧边栏切到别的页面时，边界要自动恢复。
    // 否则「首页崩了」的告警会一直盖在知识库页面上，看起来像整站都坏了。
    <ErrorBoundary key={location.pathname}>
      <AppRoutes />
    </ErrorBoundary>
  );
}
