/** 根组件：全局组装（Provider 挂载点）。 */
import { AuthProvider } from "./features/auth";
import { AppRoutes } from "./app/routes";

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
