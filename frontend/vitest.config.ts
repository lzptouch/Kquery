import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

export default mergeConfig(
  viteConfig, // 继承 @→src alias、proxy、plugins
  defineConfig({
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./test/setup.ts"],
      include: ["./test/unit/**/*.test.{ts,tsx}"],
      css: false,
      restoreMocks: true,
      clearMocks: true,
      coverage: {
        provider: "v8",
        include: ["src/stores/**", "src/services/**", "src/lib/**"],
        thresholds: { lines: 80 },
      },
    },
    server: {
      fs: {
        allow: ["..", "./"],
      },
    },
  }),
);
