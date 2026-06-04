import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// In dev, if VITE_API_PROXY is set (e.g. the deployed Function App URL or
// http://localhost:7071), requests to /api are proxied there so the app can run
// same-origin without CORS. If it is unset, api.js falls back to stub data.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.VITE_API_PROXY;

  return {
    plugins: [react()],
    server: proxyTarget
      ? {
          proxy: {
            "/api": {
              target: proxyTarget,
              changeOrigin: true,
              secure: false,
            },
          },
        }
      : {},
  };
});
