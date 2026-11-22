import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// API 与 SPA 页面共用路径前缀（/knowledge、/admin）时的分流规则：
// 浏览器导航（Accept: text/html）→ SPA 页面；API 调用（fetch/curl）→ 后端代理
const spaBypass = (req: { headers: { accept?: string } }) =>
 req.headers.accept?.includes("text/html") ? "/index.html" : undefined;

// 端口可被环境变量覆盖：同机常同时跑多个项目，8000/5173 被占时不必改代码
// 用法：KQUERY_API_TARGET=http://localhost:8010 KQUERY_DEV_PORT=5174 npm run dev
const apiProxy = {
 target: process.env.KQUERY_API_TARGET || "http://localhost:8000",
 changeOrigin: false,
 bypass: spaBypass,
};

export default defineConfig({
  plugins: [react()],
  build: {
    // 构建产物不能放在 /assets/：后端已有 /assets/{name} 用来提供图片等媒体资源
    // （stage-7 跨模态检索）。两边同名时，nginx 的正则 location 会优先匹配，
    // 把 JS/CSS 请求代理给后端，页面直接白屏且报 404，很难一眼看出是路径冲突。
    // 换一个只有前端用的前缀，两边彻底隔离。
    assetsDir: "static",
  },
  server: {
    port: Number(process.env.KQUERY_DEV_PORT) || 5173,
    host: process.env.KQUERY_DEV_HOST || "127.0.0.1",
    // 这份列表必须与 deploy/docker/frontend.nginx.conf 的 location 保持一致。
    // 曾漏了 /readyz 与 /livez：nginx（生产）代理了它们，vite（开发）没有，
    // 于是开发环境下探针请求回落到 SPA 的 index.html —— 返回 200 + HTML，
    // 看起来"通了"，实际拿不到任何探针数据（dev/prod 行为不一致很难察觉）。
    proxy: {
      "/ask": apiProxy,
      "/knowledge": apiProxy,
      "/admin": apiProxy,
      "/feedback": apiProxy,
      "/sessions": apiProxy,
      "/graph": apiProxy,
      "/search": apiProxy,
      "/media": apiProxy,
      "/assets": apiProxy,
      "/metrics": apiProxy,
      "/health": apiProxy,
      "/healthz": apiProxy,
      "/livez": apiProxy,
      "/readyz": apiProxy,
    },
  },
});
