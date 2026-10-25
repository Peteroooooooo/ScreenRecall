import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 开发：先启动 python -m screenrecall.webapi --dev-origin http://127.0.0.1:5173，再 npm run dev
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8600", changeOrigin: true } },
  },
  build: { outDir: "dist", emptyOutDir: true, assetsInlineLimit: 0 },
});
