import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Coinext UI dev/build config (canonical service table: ui -> port 3000).
//
// The dashboard talks to the `api` service (FastAPI, port 8000). In production
// the base URL is injected via VITE_API_BASE (see src/api.ts); in dev we also
// expose a `/api` proxy so the browser is not blocked by CORS when running
// `npm run dev` against a locally running api container.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 3000,
    proxy: {
      // Optional convenience proxy: set VITE_API_BASE=/api to route through it.
      "/api": {
        target: process.env.VITE_API_TARGET || "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  preview: {
    host: "0.0.0.0",
    port: 3000,
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
