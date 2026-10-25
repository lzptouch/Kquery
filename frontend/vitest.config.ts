import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 前端单测配置（B0 工程基线）。
//
// 为什么单独一份配置而不是塞进 vite.config.ts：
// vite.config.ts 里 server.proxy 是给**开发服务器**用的（把 /ask 等前缀代理到后端），
// 测试环境不需要它；而且 vitest 需要独立的 `test` 段与 jsdom 环境。
// 两者混在一起会让 `npm test` 也去读后端代理配置，多一份出错面。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.{test,spec}.{ts,tsx}"],
    // CI 上没有 watch 模式，但本地 `npm test` 必须一次跑完即退出，
    // 否则 `npm test` 会挂住 CI 步骤（看起来像超时，实际上在等输入）。
    watch: false,
  },
});
