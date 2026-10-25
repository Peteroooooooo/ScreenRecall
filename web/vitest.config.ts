import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 前端单元 / 组件测试：jsdom + Testing Library，fetch 一律在测试里模拟（不连真实服务）
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
