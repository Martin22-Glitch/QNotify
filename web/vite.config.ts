import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// dev 代理：前端用相对路径 /api 就能打到本机核心服务（手机用真机访问时走设置页填的地址）
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8720",
      "/healthz": "http://127.0.0.1:8720",
      "/ws": { target: "ws://127.0.0.1:8720", ws: true },
    },
  },
  preview: {
    host: true,
    port: 5179,
    proxy: {
      "/api": "http://127.0.0.1:8720",
      "/healthz": "http://127.0.0.1:8720",
      "/ws": { target: "ws://127.0.0.1:8720", ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
