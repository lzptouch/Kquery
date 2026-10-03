/** 根组件：只做全局组装（后续阶段在此叠加全局 Provider）。 */
import { AppRoutes } from "./app/routes";

export default function App() {
  return <AppRoutes />;
}
