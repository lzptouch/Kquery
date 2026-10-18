/**
 * features/auth 公开出口。
 *
 * 约定：features 之间禁止互相 import，跨 feature 引用一律走各自的 index.ts，
 * 这样依赖方向是单向的（routes → auth → lib），不会因为互相 import 悄悄长出环。
 */
export { AuthProvider, useAuth } from "./AuthContext";
export { AppLayout } from "./AppLayout";
export { LoginPage } from "./LoginPage";
export { NotFoundPage } from "./NotFoundPage";
export { RegisterPage } from "./RegisterPage";
export { RedirectIfAuthenticated, RequireAdmin, RequireAuth } from "./guards";
