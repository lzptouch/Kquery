import { resolve } from "node:path";
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 前端单测配置（B0 工程基线）。
//
// 为什么单独一份配置而不是塞进 vite.config.ts：
// vite.config.ts 里 server.proxy 是给**开发服务器**用的（把 /ask 等前缀代理到后端），
// 测试环境不需要它；而且 vitest 需要独立的 `test` 段与 jsdom 环境。
// 两者混在一起会让 `npm test` 也去读后端代理配置，多一份出错面。
//
// **测试文件在仓库根 `tests/frontend/`**（2026-11-19 结构整理：全部测试集中到根
// tests/，与后端 pytest 并列）。该目录下有一个相对符号链接 `node_modules ->
// ../../frontend/node_modules`，让 Node/Vite 的模块解析（含 package.json exports）
// 从测试文件所在目录向上能找到依赖。不要用 resolve.alias 指到包目录：那会绕过
// exports 映射，`@testing-library/jest-dom/vitest` 这类深路径会解析失败。
// 首次 clone 后执行 `cd frontend && npm install` 即自动生效（脚本会补链接，见
// scripts/dev/link-test-deps.mjs）。

export default defineConfig({
  plugins: [react()],
  // 测试文件在仓库根（frontend/ 之外），必须显式放行父目录，
  // 否则 Vite 的 fs 守卫会拒绝加载 —— 表现是 "Failed to load url ... Does the file exist?"。
  server: {
    fs: {
      allow: [resolve(__dirname, ".."), resolve(__dirname)],
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: [resolve(__dirname, "../tests/frontend/setup.ts")],
    include: ["../tests/frontend/**/*.{test,spec}.{ts,tsx}"],
    // tests/frontend/node_modules 是指向 frontend/node_modules 的符号链接，
    // 必须显式排除，否则会把第三方包自带的 __tests__ 当成我们的用例收集。
    exclude: ["**/node_modules/**", "**/dist/**"],
    // CI 上没有 watch 模式，但本地 `npm test` 必须一次跑完即退出，
    // 否则 `npm test` 会挂住 CI 步骤（看起来像超时，实际上在等输入）。
    watch: false,
  },
});
