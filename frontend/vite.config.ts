import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API 与 SPA 页面共用路径前缀（/knowledge、/admin）时的分流规则：
// 浏览器导航（Accept: text/html）→ SPA 页面；API 调用（fetch/curl）→ 后端代理
const spaBypass = (req: { headers: { accept?: string } }) =>
  req.headers.accept?.includes("text/html") ? "/index.html" : undefined;

const apiProxy = {
  target: "http://localhost:8000",
  changeOrigin: false,
  bypass: spaBypass,
};

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/ask": apiProxy,
      "/knowledge": apiProxy,
      "/admin": apiProxy,
      "/feedback": apiProxy,
      "/sessions": apiProxy,
      "/reports": apiProxy,
      "/graph": apiProxy,
      "/search": apiProxy,
      "/media": apiProxy,
      "/assets": apiProxy,
      "/metrics": apiProxy,
      "/health": apiProxy,
    },
  },
});
